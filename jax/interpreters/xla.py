# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import namedtuple, defaultdict
import itertools as it
import operator as op
import os

from absl import logging
import numpy as onp

from ..config import flags, bool_env
from .. import core
from .. import ad_util
from .. import tree_util
from .. import dtypes
from .. import lazy
from .. import linear_util as lu
from ..abstract_arrays import (ConcreteArray, ShapedArray, AbstractToken,
                               make_shaped_array, array_types, raise_to_shaped,
                               abstract_token)
from ..core import valid_jaxtype, Literal, pp_eqn_compact
from ..pprint_util import pp
from ..util import (partial, partialmethod, cache, safe_map, prod, unzip2,
                    memoize, extend_name_stack, wrap_name)
from ..lib import xla_bridge as xb
from ..lib import xla_client as xc
from . import partial_eval as pe
from . import ad
from . import masking

FLAGS = flags.FLAGS
flags.DEFINE_bool('jax_debug_nans',
                  bool_env('JAX_DEBUG_NANS', False),
                  'Add nan checks to every operation.')
flags.DEFINE_bool('jax_log_compiles',
                  bool_env('JAX_LOG_COMPILES', False),
                  'Print a message each time a `jit` computation is compiled.')

def _map(f, *xs): return tuple(map(f, *xs))
def identity(x): return x


### handlers

xb.register_constant_handler(core.Unit, lambda c, *_: c.Tuple())

def aval_to_xla_shape(aval):
  try:
    return xla_shape_handlers[type(aval)](aval)
  except KeyError:
    raise TypeError(f"No xla_shape_handler for type: {type(aval)}")
xla_shape_handlers = {core.AbstractUnit: lambda _: xc.Shape.tuple_shape(())}
xla_shape_handlers[ShapedArray] = lambda a: xc.Shape.array_shape(a.dtype, a.shape)
xla_shape_handlers[ConcreteArray] = lambda a: xc.Shape.array_shape(a.dtype, a.shape)

def aval_to_result_handler(device, aval):
  try:
    return xla_result_handlers[type(aval)](device, aval)
  except KeyError:
    raise TypeError(f"No xla_result_handler for type: {type(aval)}")
xla_result_handlers = {core.AbstractUnit: lambda _, __: lambda _: core.unit}
def array_result_handler(device, aval):
  return partial(DeviceArray, raise_to_shaped(aval), device, lazy.array(aval.shape))
xla_result_handlers[ShapedArray] = array_result_handler
xla_result_handlers[ConcreteArray] = array_result_handler

def device_put(x, device=None):
  x = canonicalize_dtype(x)
  try:
    return device_put_handlers[type(x)](x, device)
  except KeyError:
    raise TypeError(f"No device_put handler for type: {type(x)}")

device_put_handlers = {
    core.Unit:
    lambda _, device: xc.Buffer.make_tuple(
        (), device, backend=xb.get_device_backend(device))
}
def _device_put_array(x, device):
  return xc.Buffer.from_pyval(x, device, backend=xb.get_device_backend(device))
for _t in array_types:
  device_put_handlers[_t] = _device_put_array

def _device_put_scalar(x, device):
  return xc.Buffer.from_pyval(dtypes.coerce_to_array(x), device,
                              backend=xb.get_device_backend(device))
for _t in dtypes.python_scalar_dtypes.keys():
  device_put_handlers[_t] = _device_put_array

# TODO(mattjj): try to remove this canonicalize_dtype stuff
def canonicalize_dtype(x):
  typ = type(x)
  if handler := canonicalize_dtype_handlers.get(typ):
    return handler(x)
  for typ in typ.mro():
    if handler := canonicalize_dtype_handlers.get(typ):
      return handler(x)
  raise TypeError(f"No canonicalize_dtype handler for type: {type(x)}")

canonicalize_dtype_handlers = {core.Unit: identity}
def _canonicalize_ndarray_dtype(x):
  return onp.asarray(x, dtypes.canonicalize_dtype(dtypes.result_type(x)))
for _t in array_types:
  canonicalize_dtype_handlers[_t] = _canonicalize_ndarray_dtype
def _canonicalize_python_scalar_dtype(typ, x):
  return onp.asarray(
    x, dtypes.canonicalize_dtype(dtypes.python_scalar_dtypes[typ]))
for _t in dtypes.python_scalar_dtypes.keys():
  canonicalize_dtype_handlers[_t] = partial(_canonicalize_python_scalar_dtype, _t)

def abstractify(x):
  typ = type(x)
  if aval_fn := pytype_aval_mappings.get(typ):
    return aval_fn(x)
  for typ in typ.mro():
    if aval_fn := pytype_aval_mappings.get(typ):
      return aval_fn(x)
  raise TypeError(f"No abstraction handler for type: {type(x)}")

pytype_aval_mappings = {core.Unit: lambda _: core.abstract_unit}
for _t in array_types:
  pytype_aval_mappings[_t] = make_shaped_array

def _make_abstract_python_scalar(typ, _):
  return ShapedArray((), dtypes.python_scalar_dtypes[typ], weak_type=True)

for _t in dtypes.python_scalar_dtypes.keys():
  pytype_aval_mappings[_t] = partial(_make_abstract_python_scalar, _t)


### op-by-op execution

def arg_spec(x):
  aval = abstractify(x)
  try:
    return aval, x._device
  except:
    return aval, None

def apply_primitive(prim, *args, **params):
  """Impl rule that compiles and runs a single primitive 'prim' using XLA."""
  compiled_fun = xla_primitive_callable(prim, *map(arg_spec, args), **params)
  return compiled_fun(*args)

@cache()
def xla_primitive_callable(prim, *arg_specs, **params):
  avals, arg_devices = unzip2(arg_specs)
  device = _device_from_arg_devices(arg_devices)
  backend = xb.get_device_backend(device)
  aval_out = prim.abstract_eval(*avals, **params)
  if not prim.multiple_results:
    handle_result = aval_to_result_handler(device, aval_out)
  else:
    handlers = tuple(map(partial(aval_to_result_handler, device), aval_out))
    handle_result = lambda xs: tuple(h(x) for h, x in zip(handlers, xs.destructure()))
  tuple_args = len(avals) > 100
  if prim in initial_style_translations:
    nreps = initial_style_primitive_replicas(params)
  else:
    nreps = 1
  built_c = primitive_computation(prim, AxisEnv(nreps), backend, tuple_args,
                                  *avals, **params)
  options = xb.get_compile_options(
      num_replicas=1,
      num_partitions=1,
      device_assignment=device and (device.id,))
  compiled = built_c.Compile(compile_options=options, backend=backend)
  if nreps == 1:
    return partial(_execute_compiled_primitive, prim, compiled, backend,
                  tuple_args, handle_result)
  else:
    return partial(_execute_replicated_primitive, prim, compiled, backend,
                   tuple_args, handle_result)

def _device_from_arg_devices(devices):
  """Given devices of inputs, determine where to perform a computation.

  Args:
    devices: list where each element is a either a `Device` instance or `None`.
  Returns:
    A `Device` instance or None.
  Raises:
    ValueError if input devices are inconsistent.
  """
  try:
    device, = {d for d in devices if d is not None} or (None,)
    return device
  except ValueError:
    msg = "primitive arguments must be colocated on the same device, got {}"
    raise ValueError(msg.format(", ".join(map(str, devices))))

@cache()
def primitive_computation(prim, axis_env, backend, tuple_args, *avals, **params):
  c = xb.make_computation_builder(f"primitive_computation_{prim.name}")
  c.SetOpMetadata(xc.OpMetadata(
      op_type=prim.name,
      op_name=str(pp_eqn_compact(prim.name, params))))
  platform = xb.get_backend(backend).platform
  xla_args = _xla_callable_args(c, avals, tuple_args)
  # return val always set as a side-effect on c
  if prim in backend_specific_translations[platform]:
    rule = backend_specific_translations[platform][prim]
    rule(c, *xla_args, **params)
  elif prim in translations:
    rule = translations[prim]
    rule(c, *xla_args, **params)
  elif prim in initial_style_translations:
    rule = initial_style_translations[prim]
    rule(c, axis_env, extend_name_stack(prim.name), *xla_args, backend=backend, **params)
  else:
    raise NotImplementedError(f"XLA translation rule for {prim} not found")
  c.ClearOpMetadata()
  try:
    return c.Build()
  except RuntimeError as e:
    msg = (" ".join(map(str, e.args)) + "\n"
           "This is a bug in JAX's shape-checking rules; please report it!\n"
           "https://github.com/google/jax/issues\n")
    raise RuntimeError(msg)

def primitive_subcomputation(prim, *avals, **params):
  return primitive_computation(prim, AxisEnv(1), None, False, *avals, **params)

def _execute_compiled_primitive(prim, compiled, backend, tuple_args,
                                result_handler, *args):
  device, = compiled.local_devices()
  input_bufs = [device_put(x, device) for x in args if x is not token]
  if tuple_args:
    input_bufs = [make_tuple(input_bufs, device, backend)]
  out_buf = compiled.Execute(input_bufs)
  if FLAGS.jax_debug_nans:
    check_nans(prim, out_buf.destructure() if prim.multiple_results else out_buf)
  return result_handler(out_buf)

def _execute_replicated_primitive(prim, compiled, backend, tuple_args,
                                  result_handler, *args):
  input_bufs = [
      [device_put(x, device) for x in args if x is not token]
      for device in compiled.local_devices()]
  if tuple_args:
    input_bufs = [[make_tuple(bufs, device, backend)] for bufs, device in
                  zip(input_bufs, compiled.local_devices())]
  out_buf = compiled.ExecutePerReplica(input_bufs)[0]
  return result_handler(out_buf)

def check_nans(prim, bufs):
  if prim.multiple_results:
    for buf in bufs:
      _check_nans(prim.name, buf.shape(), buf)
  else:
    _check_nans(prim.name, bufs.shape(), bufs)

def _check_nans(name, xla_shape, buf):
  if xla_shape.is_tuple():
    assert not xla_shape.tuple_shapes()
  elif dtypes.issubdtype(xla_shape.element_type(), onp.floating):
    if onp.any(onp.isnan(buf.to_py())):
      msg = "invalid value (nan) encountered in {}"
      raise FloatingPointError(msg.format(name))

### compiling jaxprs

def prefetch(x):
  if isinstance(x, DeviceArray):
    x.copy_to_host_async()
  return x

def jaxpr_literals(jaxpr):
  """Generates all the literals inside a jaxpr, including nested subjaxprs."""
  for eqn in jaxpr.eqns:
    for v in eqn.invars:
      if type(v) is core.Literal:
        yield v.val
  for subjaxpr in core.subjaxprs(jaxpr):
    yield from jaxpr_literals(subjaxpr)


def jaxpr_subcomp(c, jaxpr, backend, axis_env, consts, name_stack, *args):
  platform = xb.get_backend(backend).platform

  def read(v):
    return c.Constant(canonicalize_dtype(v.val)) if type(v) is Literal else env[v]

  def write(v, node):
    assert node is not None
    env[v] = node

  env = {}
  write(core.unitvar, c.Tuple())
  _map(write, jaxpr.constvars, consts)
  _map(write, jaxpr.invars, args)
  for eqn in jaxpr.eqns:
    c.SetOpMetadata(xc.OpMetadata(
        op_type=eqn.primitive.name,
        op_name=str(pp(name_stack) >> pp_eqn_compact(
            eqn.primitive.name, eqn.params))))
    in_nodes = list(map(read, eqn.invars))
    if eqn.primitive in backend_specific_translations[platform]:
      rule = backend_specific_translations[platform][eqn.primitive]
      ans = rule(c, *in_nodes, **eqn.params)
    elif eqn.primitive in translations:
      ans = translations[eqn.primitive](c, *in_nodes, **eqn.params)
    elif eqn.primitive in initial_style_translations:
      new_params = check_backend_params(eqn.params, backend)
      rule = initial_style_translations[eqn.primitive]
      ans = rule(c, axis_env, extend_name_stack(name_stack, eqn.primitive.name),
                 *in_nodes, backend=backend, **new_params)
    elif eqn.primitive in parallel_translations:
      replica_groups = axis_groups(axis_env, eqn.params['axis_name'])
      new_params = {k: v for k, v in eqn.params.items() if k != 'axis_name'}
      rule = parallel_translations[eqn.primitive]
      ans = rule(c, *in_nodes, replica_groups=replica_groups, **new_params)
    elif eqn.primitive in call_translations:
      new_params = check_backend_params(eqn.params, backend)
      rule = call_translations[eqn.primitive]
      ans = rule(c, axis_env, in_nodes,
                 name_stack, backend=backend, **new_params)
    else:
      msg = "XLA translation rule for primitive '{}' not found"
      raise NotImplementedError(msg.format(eqn.primitive.name))

    c.GetShape(ans)  # force xla to do shape error checking
    out_nodes = xla_destructure(c, ans) if eqn.primitive.multiple_results else [ans]
    c.ClearOpMetadata()
    _map(write, eqn.outvars, out_nodes)
  return _map(read, jaxpr.outvars)

def xla_destructure(c, ans):
  num_elements = len(c.GetShape(ans).tuple_shapes())
  return [c.GetTupleElement(ans, i) for i in range(num_elements)]

def check_backend_params(params, outer_backend):
  # For nested calls, the outermost call sets the backend for all inner calls;
  # it's an error if the inner call has a conflicting explicit backend spec.
  inner_backend = params.get('backend', None)
  if inner_backend and inner_backend != outer_backend:
    msg = (
      "Outer-jit backend specification {} must match explicit inner-jit "
      "backend specification {}.")
    raise ValueError(msg.format(outer_backend, inner_backend))
  return {k: params[k] for k in params if k != 'backend'}


class AxisEnv(object):
  def __init__(self, nreps, names=(), sizes=(), devices=None):
    assert isinstance(names, tuple)
    assert isinstance(sizes, tuple)
    self.nreps = nreps
    self.names = names
    self.sizes = sizes
    self.devices = devices

def extend_axis_env(env, name, size):
  return AxisEnv(env.nreps, env.names + (name,), env.sizes + (size,), env.devices)

def axis_read(axis_env, axis_name):
  return max(i for i, name in enumerate(axis_env.names) if name == axis_name)

def axis_groups(axis_env, name):
  if isinstance(name, (list, tuple)):
    mesh_axes = tuple(map(partial(axis_read, axis_env), name))
  else:
    mesh_axes = (axis_read(axis_env, name),)
  return _axis_groups(axis_env.nreps, axis_env.sizes, mesh_axes)

def _axis_groups(nrep, mesh_spec, mesh_axes):
  trailing_size, ragged = divmod(nrep, prod(mesh_spec))
  assert not ragged
  full_spec = list(mesh_spec) + [trailing_size]
  iota = onp.arange(prod(full_spec)).reshape(full_spec)
  groups = onp.reshape(
      onp.moveaxis(iota, mesh_axes, onp.arange(len(mesh_axes))),
      (prod(onp.take(full_spec, mesh_axes)), -1))
  return tuple(map(tuple, groups.T))

def jaxpr_replicas(jaxpr):
  """The number of replicas needed for a jaxpr.

  For a eqn, multiply the `axis_size` with the `jaxpr_replicas` of the
  subjaxprs. For a list of eqns, take the maximum number of replicas.
  """
  return max(it.chain([1], (eqn_replicas(eqn) for eqn in jaxpr.eqns)))

# TODO(mattjj): this function assumes that only pmap has a parameter named
# axis_size, and that it corresponds to cross-replica mapping
def eqn_replicas(eqn):
  if call_jaxpr := eqn.params.get("call_jaxpr"):
    return eqn.params.get('axis_size', 1) * jaxpr_replicas(call_jaxpr)
  elif eqn.primitive in initial_style_translations:
    return initial_style_primitive_replicas(eqn.params)
  else:
    return 1

def initial_style_primitive_replicas(params):
  nums = (jaxpr_replicas(param if type(param) is core.Jaxpr else param.jaxpr)
          for param in params.values()
          if type(param) in (core.Jaxpr, core.TypedJaxpr))
  return max(it.chain([1], nums))

# TODO(mattjj,skyewm): the functions here are utilities for checking if
# not-yet-supported features are used with multi-host programming

def jaxpr_has_pmap(jaxpr):
  """Whether there is an xla_pmap primitive anywhere inside a Jaxpr."""
  for eqn in jaxpr.eqns:
    if 'xla_pmap' in eqn.primitive.name:
      return True
  return any(jaxpr_has_pmap(subjaxpr) for subjaxpr in core.subjaxprs(jaxpr))


def jaxpr_collectives(jaxpr):
  """Generates all the collective primitives anywhere inside a Jaxpr."""
  for eqn in jaxpr.eqns:
    if eqn.primitive in parallel_translations:
      yield eqn.primitive
  for subjaxpr in core.subjaxprs(jaxpr):
    yield from jaxpr_collectives(subjaxpr)


### xla_call underlying jit

def _xla_call_impl(fun, *args, device, backend, name):
  compiled_fun = _xla_callable(fun, device, backend, name, *map(arg_spec, args))
  try:
    return compiled_fun(*args)
  except FloatingPointError:
    print("Invalid value encountered in the output of a jit function. "
          "Calling the de-optimized version.")
    return fun.call_wrapped(*args)  # probably won't return

@lu.cache
def _xla_callable(fun, device, backend, name, *arg_specs):
  if device is not None and backend is not None:
    raise ValueError(
        f"can't specify both a device and a backend for jit, got device={device} and backend={backend}"
    )

  abstract_args, arg_devices = unzip2(arg_specs)
  pvals = [pe.PartialVal((aval, core.unit)) for aval in abstract_args]
  jaxpr, pvals, consts = pe.trace_to_jaxpr(
      fun, pvals, instantiate=False, stage_out_calls=True, bottom=True)

  _map(prefetch, it.chain(consts, jaxpr_literals(jaxpr)))

  nreps = jaxpr_replicas(jaxpr)
  device = _xla_callable_device(nreps, backend, device, arg_devices)
  result_handlers = tuple(map(partial(_pval_to_result_handler, device), pvals))

  # Computations that only produce constants and/or only rearrange their inputs,
  # which are often produced from partial evaluation, don't need compilation,
  # and don't need to force their (potentially lazy) arguments.
  if not jaxpr.eqns:
    device = device or xb.get_backend(None).get_default_device_assignment(1)[0]
    return partial(_execute_trivial, jaxpr, device, consts, result_handlers)

  log_priority = logging.WARNING if FLAGS.jax_log_compiles else logging.DEBUG
  logging.log(log_priority,
              f"Compiling {fun.__name__} for args {abstract_args}.")

  if nreps > xb.device_count(backend):
    msg = ("compiling computation that requires {} replicas, but only {} XLA "
            "devices are available")
    raise ValueError(msg.format(nreps, xb.device_count(backend)))
  if xb.host_count() > 1 and (nreps > 1 or jaxpr_has_pmap(jaxpr)):
    raise NotImplementedError(
        "jit of multi-host pmap not implemented (and jit-of-pmap can cause "
        "extra data movement anyway, so maybe you don't want it after all).")

  tuple_args = len(abstract_args) > 100  # pass long arg lists as tuple for TPU

  c = xb.make_computation_builder(f"jit_{fun.__name__}")
  xla_consts = _map(c.Constant, consts)
  xla_args = _xla_callable_args(c, abstract_args, tuple_args)
  out_nodes = jaxpr_subcomp(
      c, jaxpr, backend, AxisEnv(nreps, (), ()), xla_consts,
      extend_name_stack(wrap_name(name, 'jit')), *xla_args)
  built = c.Build(c.Tuple(*out_nodes))

  options = xb.get_compile_options(
      num_replicas=nreps,
      num_partitions=1,
      device_assignment=(device.id,) if device else None)
  compiled = built.Compile(compile_options=options, backend=xb.get_backend(backend))

  if nreps == 1:
    return partial(_execute_compiled, compiled, backend, result_handlers, tuple_args)
  else:
    return partial(_execute_replicated, compiled, backend, result_handlers, tuple_args)

def _xla_callable_device(nreps, backend, device, arg_devices):
  if nreps > 1:
    if device is None and backend is None:
      return None
    else:
      raise ValueError(
          f"can't specify device or backend for jit-of-pmap, got device={device} and backend={backend}"
      )
  elif device is None and backend is None:
    return _device_from_arg_devices(arg_devices)
  elif device is not None and backend is None:
    return device
  elif device is None:
    return xb.get_backend(backend).get_default_device_assignment(1)[0]
  else:
    assert False  # Unreachable given the error check in _xla_callable

def _xla_callable_args(c, avals, tuple_args):
  if not tuple_args:
    xla_args = [c.ParameterWithShape(aval_to_xla_shape(a))
                if a is not abstract_token else c.CreateToken() for a in avals]
  else:
    tuple_param = c.ParameterWithShape(xc.Shape.tuple_shape(
        [aval_to_xla_shape(a) for a in avals if a is not abstract_token]))
    xla_inputs = iter(xla_destructure(c, tuple_param))
    xla_args = [next(xla_inputs) if a is not abstract_token else c.CreateToken()
                for a in avals]
    assert next(xla_inputs, None) is None

  return xla_args

def _pval_to_result_handler(device, pval):
  pv, const = pval
  if pv is not None:
    return aval_to_result_handler(device, pv)
  const = _device_put_impl(const, device) if device else const
  return lambda _: const

def _execute_compiled(compiled, backend, handlers, tuple_args, *args):
  device, = compiled.local_devices()
  input_bufs = [device_put(x, device) for x in args if x is not token]
  if tuple_args:
    input_bufs = [make_tuple(input_bufs, device, backend)]
  out_bufs = compiled.Execute(input_bufs).destructure()
  if FLAGS.jax_debug_nans: check_nans(xla_call_p, out_bufs)
  return [handler(out_buf) for handler, out_buf in zip(handlers, out_bufs)]

def _execute_replicated(compiled, backend, handlers, tuple_args, *args):
  input_bufs = [
      [device_put(x, device) for x in args if x is not token]
      for device in compiled.local_devices()]
  if tuple_args:
    input_bufs = [[make_tuple(bufs, device, backend)] for bufs, device in
                  zip(input_bufs, compiled.local_devices())]
  out_bufs = compiled.ExecutePerReplica(input_bufs)[0].destructure()
  if FLAGS.jax_debug_nans: check_nans(xla_call_p, out_bufs)
  return [handler(out_buf) for handler, out_buf in zip(handlers, out_bufs)]

def _execute_trivial(jaxpr, device, consts, handlers, *args):
  env = {core.unitvar : core.unit}
  _map(env.setdefault, jaxpr.invars, args)
  _map(env.setdefault, jaxpr.constvars, consts)
  outs = [canonicalize_dtype(v.val) if type(v) is Literal else env[v]
          for v in jaxpr.outvars]
  return [_copy_device_array_to_device(x, device) if type(x) is DeviceArray
          else h(device_put(x, device)) for h, x in zip(handlers, outs)]

def make_tuple(bufs, device, backend):
  return xb.get_backend(backend).make_tuple(bufs, device)

@memoize
def _get_device(device, backend):
  # TODO(mattjj): after jaxlib update, avoid compile here, just to get device
  c = xb.make_computation_builder("get_device")
  built = c.Build(c.Tuple())
  options = xb.get_compile_options(
      num_replicas=1,
      num_partitions=1,
      device_assignment=(device.id,) if device else None)
  compiled = built.Compile(compile_options=options, backend=xb.get_backend(backend))
  out, = compiled.local_devices()
  return out

xla_call_p = core.Primitive('xla_call')
xla_call_p.call_primitive = True
xla_call_p.multiple_results = True
xla_call = partial(core.call_bind, xla_call_p)
xla_call_p.def_custom_bind(xla_call)
xla_call_p.def_impl(_xla_call_impl)

def _xla_call_translation_rule(c, axis_env,
                               in_nodes, name_stack, backend, name,
                               call_jaxpr, device=None):
  del device  # Ignored.
  subc = xb.make_computation_builder(f"jit_{name}")
  args = [subc.ParameterWithShape(c.GetShape(n)) for n in in_nodes]
  out_nodes = jaxpr_subcomp(subc, call_jaxpr, backend, axis_env, (),
                            extend_name_stack(name_stack, wrap_name(name, 'jit')), *args)
  subc = subc.Build(subc.Tuple(*out_nodes))
  return c.Call(subc, list(in_nodes))
ad.primitive_transposes[xla_call_p] = partial(ad.call_transpose, xla_call_p)


parallel_translations = {}
initial_style_translations = {}
backend_specific_translations = defaultdict(dict)

translations = {core.identity_p: lambda c, x: x}
call_translations = {xla_call_p: _xla_call_translation_rule}

def zeros_like_translation_rule(c, x):
  shape = c.GetShape(x)
  if shape.is_tuple():
    assert not shape.tuple_shapes()
    return c.Tuple()
  else:
    zero = c.Constant(onp.array(0, shape.element_type()))
    return c.Broadcast(zero, shape.dimensions())
translations[ad_util.zeros_like_p] = zeros_like_translation_rule

def add_jaxvals_translation_rule(c, x, y):
  shape = c.GetShape(x)
  if not shape.is_tuple():
    return c.Add(x, y)
  assert not shape.tuple_shapes()
  return x
translations[ad_util.add_jaxvals_p] = add_jaxvals_translation_rule

def lower_fun(fun, instantiate=False, initial_style=False):
  """Build a translation rule for a traceable function."""
  def f(c, *args, **params):
    backend = params.pop('backend', None)
    # TODO(mattjj): revise this 'calling convention'
    if initial_style:
      axis_env, name_stack, xla_args = args[0], args[1], args[2:]
    else:
      axis_env, name_stack, xla_args = AxisEnv(1), '', args
    xla_shapes = tuple(map(c.GetShape, xla_args))
    avals = map(_aval_from_xla_shape, xla_shapes)
    pvals = [pe.PartialVal((a, core.unit)) for a in avals]
    jaxpr, _, consts = pe.trace_to_jaxpr(
        lu.wrap_init(fun, params), pvals, instantiate=True)
    consts = _map(c.Constant, consts)
    outs = jaxpr_subcomp(c, jaxpr, backend, axis_env, consts,
                         name_stack, *xla_args)
    return c.Tuple(*outs)
  return f

def _aval_from_xla_shape(xla_shape):
  if xla_shape.is_tuple() and not xla_shape.tuple_shapes():
    return core.abstract_unit
  else:
    return ShapedArray(xla_shape.dimensions(), xla_shape.element_type())


### device-persistent data

class Token(object): pass
token = Token()

pytype_aval_mappings[Token] = lambda _: abstract_token
core.pytype_aval_mappings[Token] = lambda _: abstract_token
xla_shape_handlers[AbstractToken] = lambda _: xc.Shape.token_shape()
xla_result_handlers[AbstractToken] = lambda _, __: lambda _: token
canonicalize_dtype_handlers[Token] = identity


class DeviceValue(object):
  """A DeviceValue represents a value backed by device memory."""
  __slots__ = ["aval", "device_buffer", "__weakref__"]

  def __init__(self, aval, device_buffer):
    self.aval = aval
    self.device_buffer = device_buffer

  def _check_if_deleted(self):
    if self.device_buffer is deleted_buffer:
      raise ValueError("DeviceValue has been deleted.")

  def block_until_ready(self):
    """Blocks the caller until the buffer's value has been computed on device.

    This method is mostly useful for timing microbenchmarks that wish to
    time how long a computation takes, without transferring the result back
    to the host.

    Returns the buffer object (`self`).
    """
    self._check_if_deleted()
    self.device_buffer.block_host_until_ready()
    return self

def _forward_method(attrname, self, fun, *args):
  return fun(getattr(self, attrname), *args)
_forward_to_value = partial(_forward_method, "_value")

class DeviceArray(DeviceValue):
  """A DeviceArray is an ndarray backed by a single device memory buffer."""
  # We don't subclass ndarray because that would open up a host of issues,
  # but lax_numpy.py overrides isinstance behavior and attaches ndarray methods.
  __slots__ = ["_npy_value", "_device", "_lazy_expr"]
  __array_priority__ = 100

  def __init__(self, aval, device, lazy_expr, device_buffer):
    self.aval = aval
    self.device_buffer = device_buffer
    self._device = device
    self._lazy_expr = lazy_expr

    self._npy_value = None
    if not core.skip_checks:
      assert type(aval) is ShapedArray
      npy_value = self._value
      assert npy_value.dtype == aval.dtype and npy_value.shape == aval.shape

  @property
  def _value(self):
    self._check_if_deleted()
    if self._npy_value is None:
      if is_device_constant(self):
        self._npy_value = lazy.eval_lexpr(self._lazy_expr, None)
      else:
        self._npy_value = _force(self).device_buffer.to_py()
      self._npy_value.flags.writeable = False
    return self._npy_value

  @property
  def shape(self):
    return self.aval.shape

  @property
  def dtype(self):
    return self.aval.dtype

  @property
  def size(self):
    return prod(self.aval.shape)

  @property
  def ndim(self):
    return len(self.aval.shape)

  def copy(self):
    """Returns an ndarray (backed by host memory, not device memory)."""
    return onp.asarray(self)

  def copy_to_host_async(self):
    """Requests a copy of the buffer to the host."""
    self._check_if_deleted()
    if self._npy_value is None and not is_device_constant(self):
      self.device_buffer.copy_to_host_async()

  def delete(self):
    """Deletes the device array and any cached copy on the host.

    It is an error to access the contents of a `DeviceArray` after it has
    been deleted.

    Use of this method is optional; device buffers will be reclaimed
    automatically by Python when a DeviceArray object is garbage collected.
    However, it is sometimes useful to have more explicit control over the
    time of deletion.
    """
    self.device_buffer.delete()
    self.device_buffer = deleted_buffer
    self._npy_value = None

  def __repr__(self):
    line_width = onp.get_printoptions()['linewidth']
    prefix = f'{self.__class__.__name__}('
    s = onp.array2string(self._value, prefix=prefix, suffix=',',
                         separator=', ', max_line_width=line_width)
    dtype_str = f'dtype={self.dtype.name})'
    last_line_len = len(s) - s.rfind('\n') + 1
    sep = ' '
    if last_line_len + len(dtype_str) + 1 > line_width:
      sep = ' ' * len(prefix)
    return f"{prefix}{s},{sep}{dtype_str}"

  def item(self):
    if dtypes.issubdtype(self.dtype, onp.complexfloating):
      return complex(self)
    elif dtypes.issubdtype(self.dtype, onp.floating):
      return float(self)
    elif dtypes.issubdtype(self.dtype, onp.integer):
      return int(self)
    elif dtypes.issubdtype(self.dtype, onp.bool_):
      return bool(self)
    else:
      raise TypeError(self.dtype)

  def __len__(self):
    try:
      return self.aval.shape[0]
    except IndexError:
      raise TypeError("len() of unsized object")  # same as numpy error

  def __iter__(self):
    if self.ndim == 0:
      raise TypeError("iteration over a 0-d array")  # same as numpy error
    else:
      return self._value.__iter__()

  def __reversed__(self):
    if self.ndim == 0:
      raise TypeError("iteration over a 0-d array")
    else:
      return reversed(self._value)

  def __format__(self, format_spec):
    # Simulates behavior of https://github.com/numpy/numpy/pull/9883
    if self.ndim == 0:
      return format(self._value[()], format_spec)
    else:
      return format(self._value, format_spec)

  def __array__(self, dtype=None, context=None):
    return onp.asarray(self._value, dtype=dtype)

  @property
  def __cuda_array_interface__(self):
    return _force(self).device_buffer.__cuda_array_interface__

  __str__ = partialmethod(_forward_to_value, str)
  __bool__ = __nonzero__ = partialmethod(_forward_to_value, bool)
  def __float__(self): return self._value.__float__()
  def __int__(self): return self._value.__int__()
  def __complex__(self): return self._value.__complex__()
  __hex__ = partialmethod(_forward_to_value, hex)
  __oct__ = partialmethod(_forward_to_value, oct)
  __index__ = partialmethod(_forward_to_value, op.index)

  # pickle saves and loads just like an ndarray
  __reduce__ = partialmethod(_forward_to_value, op.methodcaller("__reduce__"))

  # clobbered when jax.numpy is imported, but useful in tests
  def __eq__(self, other): return self._value == other

  def __hash__(self):
    raise TypeError("JAX DeviceArray, like numpy.ndarray, is not hashable.")

  # The following methods are dynamically overridden in lax_numpy.py.
  def __getitem__(self, i): raise NotImplementedError

class DeletedBuffer(object): pass
deleted_buffer = DeletedBuffer()

class DeviceConstant(object):
  __slots__ = ["_device"]
  def __init__(self, device=None): self._device = device
  def device(self): return self._device
  def to_py(self): return None

def is_device_constant(x):
  return type(x) is DeviceArray and type(x.device_buffer) is DeviceConstant

core.literalable_types.add(DeviceArray)
core.pytype_aval_mappings[DeviceArray] = ConcreteArray
pytype_aval_mappings[DeviceArray] = op.attrgetter('aval')
canonicalize_dtype_handlers[DeviceArray] = identity

def _device_array_constant_handler(c, val, canonicalize_types=True):
  if is_device_constant(val):
    return lazy.stage_lexpr(c, val._lazy_expr, None)
  base_val = c.Constant(val.device_buffer.to_py())
  return lazy.stage_lexpr(c, val._lazy_expr, base_val)
xb.register_constant_handler(DeviceArray, _device_array_constant_handler)

def _device_put_device_array(x, device):
  x = _copy_device_array_to_device(x, device)
  return _force(x).device_buffer
device_put_handlers[DeviceArray] = _device_put_device_array

def _copy_device_array_to_device(x, device):
  if is_device_constant(x):
    return DeviceArray(x.aval, device, x._lazy_expr, DeviceConstant(device))
  elif xb.get_device_backend(device).platform == x.device_buffer.platform():
    if device is None or x.device_buffer.device() == device:
      return x
    else:
      moved_buf = x.device_buffer.copy_to_device(device)
  else:
    # Buffers from different XLA backends are passed through the host.
    moved_buf = xc.Buffer.from_pyval(x.device_buffer.to_py(), device,
                                     backend=xb.get_device_backend(device))
  return DeviceArray(x.aval, device, x._lazy_expr, moved_buf)

def _force(x):
  if lazy.is_trivial(x._lazy_expr):
    return x
  # force x on the device where it lives, but preserve stickiness on result
  if x._device:
    device = x._device
    sticky = True
  else:
    device = x.device_buffer.device()
    sticky = False
  force_fun = _lazy_force_computation(sticky, x.aval, device, x._lazy_expr)
  return force_fun(x)

@cache()
def _lazy_force_computation(sticky, aval, device, lexpr):
  c = xb.make_computation_builder("lazy_force")
  if lazy.is_constant(lexpr):
    param = None
  else:
    idxs = [(src, dst) for dst, src in enumerate(lexpr.dims) if src is not None]
    param_shape = [None] * len(idxs)
    for src, dst in idxs:
      param_shape[src] = aval.shape[dst]
    param = c.ParameterWithShape(xc.Shape.array_shape(aval.dtype, param_shape))
  xla_out = lazy.stage_lexpr(c, lexpr, param)
  built_c = c.Build(xla_out)

  device = _device_from_arg_devices([device])
  options = xb.get_compile_options(
      num_replicas=1,
      num_partitions=1,
      device_assignment=device and (device.id,))
  backend = xb.get_device_backend(device)
  compiled = built_c.Compile(compile_options=options, backend=backend)

  result_device = device if sticky else None
  handler = partial(DeviceArray, aval, result_device, lazy.array(aval.shape))
  return ((lambda _: handler(compiled.Execute([]))) if lazy.is_constant(lexpr)
          else (lambda x: handler(compiled.Execute([x.device_buffer]))))


def _device_put_impl(x, device=None):
  if type(x) is DeviceArray:
    return _copy_device_array_to_device(x, device)

  try:
    a = abstractify(x)
  except TypeError:
    raise TypeError(f"Argument '{x}' of type {type(x)} is not a valid JAX type")
  handler = aval_to_result_handler(device, a)
  return handler(device_put(x, device))

device_put_p = core.Primitive('device_put')
device_put_p.def_impl(_device_put_impl)
pe.custom_partial_eval_rules[device_put_p] = lambda trace, x, **params: x
ad.deflinear(device_put_p, lambda cotangent, **kwargs: [cotangent])
masking.shape_rules[device_put_p] = lambda x, **_: x.shape
masking.defvectorized(device_put_p)


def _remat_translation_rule(c, axis_env, in_nodes,
                            name_stack, backend, name, call_jaxpr,
                            device=None, concrete=None):
  # This looks a lot like _xla_call_translation_rule, except for a widget we use
  # to foil CSE.
  del device, concrete  # Unused.
  subc = xb.make_computation_builder("remat_call_subcomputation")
  args = [subc.ParameterWithShape(c.GetShape(n)) for n in in_nodes]
  args = [_foil_cse(subc, x) for x in args]
  out_nodes = jaxpr_subcomp(subc, call_jaxpr, backend, axis_env, (),
                            extend_name_stack(name_stack, wrap_name(name, 'remat')), *args)
  subc = subc.Build(subc.Tuple(*out_nodes))
  return c.Call(subc, list(in_nodes))
call_translations[pe.remat_call_p] = _remat_translation_rule

def _foil_cse(c, x):
  xla_shape = c.GetShape(x)
  if xla_shape.is_tuple():
    assert not xla_shape.tuple_shapes()
    return x
  else:
    rng = c.RngUniform(c.Constant(onp.array(0, dtype=onp.float32)),
                       c.Constant(onp.array(1, dtype=onp.float32)),
                       [])
    pred = c.Lt(rng, c.Constant(onp.array(2, dtype=onp.float32)))
    shape, dtype = xla_shape.dimensions(), xla_shape.numpy_dtype()
    zero = c.Broadcast(c.Constant(onp.array(0, dtype=dtype)), shape)
    return c.Select(pred, x, zero)
