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


import itertools as it
from collections import namedtuple, Counter, defaultdict
import contextlib
import threading
from weakref import ref

import numpy as onp

from .. import core
from .. import linear_util as lu
from ..abstract_arrays import ShapedArray, ConcreteArray, raise_to_shaped
from ..util import (unzip2, safe_zip, safe_map, toposort, partial, split_list,
                    wrap_name, cache)
from ..core import (Trace, Tracer, new_master, Jaxpr, Literal, get_aval,
                    AbstractValue, unit, unitvar, abstract_unit, Primitive,
                    call_p, TypedJaxpr, new_jaxpr_eqn)

map = safe_map
zip = safe_zip
def identity(x): return x

# A partial value (pval) is modeled as a pair (pv, const), as per
#   type PVal = (PV, Const)
#   data PV = Known | Unknown AbstractValue
#   type Const = MaybeTraced JaxType
# where the Known arm, represented by a None, indicates a known (constant) value
# and the Unknown arm, represented by an AbstractValue instance, indicates an
# unknown value.
# When the pv is an AbstractValue, then the const must be unit.


class JaxprTrace(Trace):
  def pure(self, val):
    return self.new_const(val)

  def lift(self, val):
    return self.new_const(val)

  def sublift(self, val):
    return JaxprTracer(self, val.pval, FreeVar(val))

  def new_const(self, val):
    if isinstance(val, Tracer) and val._trace.level == self.level:
      raise Exception
    return JaxprTracer(self, PartialVal((None, val)), unit)

  def new_instantiated_literal(self, val):
    return JaxprTracer(self, PartialVal((get_aval(val), unit)), Literal(val))

  def new_instantiated_const(self, val):
    return JaxprTracer(self, PartialVal((get_aval(val), unit)), ConstVar(val))

  def new_arg(self, pval):
    _, const = pval
    return JaxprTracer(self, pval, LambdaBinding())

  def instantiate_const(self, tracer):
    pv, const = tracer.pval
    if isinstance(pv, AbstractValue):
      return tracer
    elif pv is None:
      if type(const) in core.literalable_types and onp.shape(const) == ():
        return self.new_instantiated_literal(const)
      else:
        return self.new_instantiated_const(const)
    else:
      raise TypeError(pv)

  def instantiate_const_abstracted(self, tracer):
    pv, const = tracer.pval
    if isinstance(pv, AbstractValue):
      return tracer
    elif pv is None:
      aval = raise_to_shaped(get_aval(const), onp.isscalar(const))
      return JaxprTracer(self, PartialVal((aval, unit)), ConstVar(const))
    else:
      raise TypeError(pv)

  def process_primitive(self, primitive, tracers, params):
    if primitive in custom_partial_eval_rules:
      return custom_partial_eval_rules[primitive](self, *tracers, **params)
    else:
      return self.default_process_primitive(primitive, tracers, params)

  def default_process_primitive(self, primitive, tracers, params):
    pvs, consts = unzip2(t.pval for t in tracers)
    if all(pv is None for pv in pvs):
      return primitive.bind(*consts, **params)
    tracers = map(self.instantiate_const, tracers)
    avals = [t.aval for t in tracers]
    out_aval = primitive.abstract_eval(*avals, **params)
    if primitive.multiple_results:
      out_tracers = [JaxprTracer(self, PartialVal((aval, unit)), None)
                     for aval in out_aval]
      eqn = new_eqn_recipe(tracers, out_tracers, primitive, params)
      for t in out_tracers: t.recipe = eqn
      return out_tracers
    else:
      out_tracer = JaxprTracer(self, PartialVal((out_aval, unit)), None)
      out_tracer.recipe = new_eqn_recipe(tracers, [out_tracer], primitive, params)
      return out_tracer

  def process_call(self, call_primitive, f, tracers, params):
    name = params.get('name', f.__name__)
    if self.master.trace_type is StagingJaxprTrace:
      tracers = map(self.instantiate_const_abstracted, tracers)
    else:
      name = wrap_name(name, 'pe')
    params = dict(params, name=name)
    if call_primitive in call_partial_eval_rules:
      return call_partial_eval_rules[call_primitive](self, f, tracers, params)
    if call_primitive in map_primitives:
      return self.process_map(call_primitive, f, tracers, params)
    in_pvs, in_consts = unzip2([t.pval for t in tracers])
    fun, aux = partial_eval(f, self, in_pvs)
    out_flat = call_primitive.bind(fun, *in_consts, **params)
    out_pvs, jaxpr, env = aux()
    env_tracers = map(self.full_raise, env)
    out_pv_consts, consts = split_list(out_flat, [len(out_flat)-len(jaxpr.constvars)])
    const_tracers = map(self.new_instantiated_const, consts)
    lifted_jaxpr = convert_constvars_jaxpr(jaxpr)
    out_tracers = [JaxprTracer(self, PartialVal((out_pv, out_pv_const)), None)
                   for out_pv, out_pv_const in zip(out_pvs, out_pv_consts)]
    new_params = dict(params, call_jaxpr=lifted_jaxpr)
    # The `jaxpr` already contains the env_vars at start of invars
    eqn = new_eqn_recipe(tuple(it.chain(const_tracers, env_tracers, tracers)),
                         out_tracers, call_primitive, new_params)
    for t in out_tracers:
      t.recipe = eqn
    return out_tracers

  def process_map(self, map_primitive, f, tracers, params):
    in_pvs, in_consts = unzip2([t.pval for t in tracers])
    reduced_pvs = [None if pv is None else _mapped_aval(pv) for pv in in_pvs]
    fun, aux = partial_eval(f, self, reduced_pvs)
    out_flat = map_primitive.bind(fun, *in_consts, **params)
    out_pvs_reduced, jaxpr, env = aux()
    out_pv_consts, consts = split_list(out_flat, [len(out_flat)-len(jaxpr.constvars)])
    out_pvs = [None if pv is None else _unmapped_aval(params['axis_size'], pv)
               for pv in out_pvs_reduced]
    const_tracers = map(self.new_instantiated_const, consts)
    env_tracers = map(self.full_raise, env)
    lifted_jaxpr = convert_constvars_jaxpr(jaxpr)
    out_tracers = [JaxprTracer(self, PartialVal((out_pv, out_pv_const)), None)
                   for out_pv, out_pv_const in zip(out_pvs, out_pv_consts)]
    # The `jaxpr` already contains the env_vars at start of invars
    new_params = dict(params,
                      mapped_invars=tuple([True] * len(const_tracers) +
                                          [False] * len(env_tracers) +
                                          [True] * len(tracers)),
                      call_jaxpr=lifted_jaxpr)
    eqn = new_eqn_recipe(tuple(it.chain(const_tracers, env_tracers, tracers)),
                         out_tracers, map_primitive, new_params)
    for t in out_tracers:
      t.recipe = eqn
    return out_tracers

  def post_process_call(self, call_primitive, out_tracers, params):
    if call_primitive in map_primitives:
      return self.post_process_map(call_primitive, out_tracers, params)
    jaxpr, consts, env = tracers_to_jaxpr([], out_tracers)
    out_pvs, out_pv_consts = unzip2(t.pval for t in out_tracers)
    out = out_pv_consts + consts
    del consts, out_pv_consts
    master = self.master
    def todo(x):
      n = len(jaxpr.outvars)
      out_pv_consts, consts = x[:n], x[n:]
      trace = JaxprTrace(master, core.cur_sublevel())
      const_tracers = map(trace.new_instantiated_const, consts)
      env_tracers = map(trace.full_raise, env)
      lifted_jaxpr = convert_constvars_jaxpr(jaxpr)
      out_tracers = [JaxprTracer(trace, PartialVal((out_pv, out_pv_const)), None)
                     for out_pv, out_pv_const in zip(out_pvs, out_pv_consts)]
      new_params = dict(params, call_jaxpr=lifted_jaxpr)
      # The `jaxpr` already contains the env_vars at start of invars
      eqn = new_eqn_recipe(tuple(it.chain(const_tracers, env_tracers)),
                           out_tracers, call_primitive, new_params)
      for t in out_tracers:
        t.recipe = eqn
      return out_tracers
    return out, todo

  def post_process_map(self, map_primitive, out_tracers, params):
    jaxpr, consts, env = tracers_to_jaxpr([], out_tracers)
    out_pvs_reduced, out_pv_consts = unzip2(t.pval for t in out_tracers)
    out_pvs = [None if pv is None else _unmapped_aval(params['axis_size'], pv)
               for pv in out_pvs_reduced]
    out = out_pv_consts + consts
    del consts, out_pv_consts
    master = self.master
    def todo(x):
      n = len(jaxpr.outvars)
      out_pv_consts, consts = x[:n], x[n:]
      trace = JaxprTrace(master, core.cur_sublevel())
      const_tracers = map(trace.new_instantiated_const, consts)
      # The `jaxpr` already contains the env_vars at start of invars
      lifted_jaxpr = convert_constvars_jaxpr(jaxpr)
      out_tracers = [JaxprTracer(trace, PartialVal((out_pv, out_pv_const)), None)
                     for out_pv, out_pv_const in zip(out_pvs, out_pv_consts)]
      new_params = dict(params,
                        mapped_invars=tuple([True] * len(const_tracers) +
                                            [False] * len(env)),
                        call_jaxpr=lifted_jaxpr)
      env_tracers = map(trace.full_raise, env)
      eqn = new_eqn_recipe(it.chain(const_tracers, env_tracers),
                           out_tracers, map_primitive, new_params)
      for t in out_tracers:
        t.recipe = eqn
      return out_tracers
    return out, todo

# This subclass is used just for its type tag, which switches the behavior of
# process_call to stage out into the jaxpr any call primitives encountered
# (rather than doing partial evaluation into the call).
class StagingJaxprTrace(JaxprTrace):
  pass

def _mapped_aval(aval):
  if aval is core.abstract_unit:
    return aval
  elif isinstance(aval, ShapedArray):
    # might be raising abstraction level from Concrete here
    return ShapedArray(aval.shape[1:], aval.dtype)
  else:
    raise TypeError(aval)

def _unmapped_aval(size, aval):
  if aval is core.abstract_unit:
    return aval
  elif isinstance(aval, ShapedArray):
    return ShapedArray((size,) + aval.shape, aval.dtype)
  else:
    raise TypeError(aval)

map_primitives = set()
custom_partial_eval_rules = {}
call_partial_eval_rules = {}


def partial_eval(f, trace, pvs):
  f = trace_to_subjaxpr(f, trace.master, False)
  return partial_eval_wrapper(f, tuple(pvs))


@lu.transformation_with_aux
def partial_eval_wrapper(avals, *consts):
  py_args = (map(PartialVal, zip(avals, consts)),)
  jaxpr, (out_pvals, consts, env) = yield py_args, {}
  out_pvs, out_consts = unzip2(out_pvals)
  out = tuple(out_consts) + tuple(consts)  # TODO: can consts be traced?
  yield out, (out_pvs, jaxpr, env)


def abstract_eval_fun(fun, *avals, **params):
  pvals_in = [PartialVal((a, unit)) for a in avals]
  _, pvals_out, _ = trace_to_jaxpr(lu.wrap_init(fun, params), pvals_in,
                                  instantiate=True)
  avals_out, _ = unzip2(pvals_out)
  for aval_out in avals_out:
    assert isinstance(aval_out, AbstractValue)  # instantiate=True
  return avals_out


class JaxprTracer(Tracer):
  __slots__ = ['pval', 'recipe']

  def __init__(self, trace, pval, recipe):
    assert isinstance(pval, PartialVal)
    pv, const = pval
    if isinstance(const, Tracer) and const._trace.level >= trace.level:
      trace.escaped_tracer_error(
          f"Tracer from a higher level: {const} in trace {trace}")
    self._trace = trace
    self.pval = pval
    self.recipe = recipe

  def __repr__(self):
    return f'Traced<{self.aval}:{self._trace}>'

  @property
  def aval(self):
    pv, const = self.pval
    return partial_val_aval(pv, const)

  @property
  def parents(self):
    return self.recipe.invars if isinstance(self.recipe, JaxprEqnRecipe) else []

  def ispure(self):
    pv, _ = self.pval
    return pv is None  # or pv is core.abstract_unit

  def full_lower(self):
    if not self.ispure():
      return self
    _, const = self.pval
    return core.full_lower(const)

class PartialVal(tuple):
  def __new__(cls, xs):
    pv, const = xs
    if not core.skip_checks:
      # type checks
      assert isinstance(pv, valid_pv_types), xs
      assert isinstance(const, core.Tracer) or core.valid_jaxtype(const), xs
      # invariant checks
      if isinstance(pv, AbstractValue):
        assert const == core.unit, xs
    return tuple.__new__(cls, xs)

valid_pv_types = (AbstractValue, type(None))

def merge_pvals(val, pval):
  pv, const = pval
  if isinstance(pv, AbstractValue):
    return val
  elif pv is None:
    return const
  else:
    raise TypeError(pv)

def partial_val_aval(pv, const):
  if isinstance(pv, AbstractValue):
    return pv
  elif pv is None:
    return get_aval(const)
  else:
    raise TypeError(pv)

def trace_to_jaxpr(fun, pvals, instantiate=False, stage_out_calls=False, bottom=False):
  """Traces a function, given abstract inputs, to a jaxpr."""
  trace_type = StagingJaxprTrace if stage_out_calls else JaxprTrace
  with new_master(trace_type, bottom=bottom) as master:
    fun = trace_to_subjaxpr(fun, master, instantiate)
    jaxpr, (out_pvals, consts, env) = fun.call_wrapped(pvals)
    assert not env
    del master

  return jaxpr, out_pvals, consts

@lu.transformation
def trace_to_subjaxpr(master, instantiate, pvals):
  assert all(isinstance(pv, PartialVal) for pv in pvals), pvals
  trace = JaxprTrace(master, core.cur_sublevel())
  in_tracers = map(trace.new_arg, pvals)
  ans = yield in_tracers, {}
  instantiate = [instantiate] * len(ans) if type(instantiate) is bool else instantiate
  out_tracers = map(trace.full_raise, map(core.full_lower, ans))
  out_tracers = map(partial(instantiate_const_at, trace), instantiate, out_tracers)
  jaxpr, consts, env = tracers_to_jaxpr(in_tracers, out_tracers)
  out_pvals = [t.pval for t in out_tracers]
  del trace, in_tracers, out_tracers
  yield jaxpr, (out_pvals, consts, env)

def instantiate_const_at(trace, instantiate, tracer):
  assert type(instantiate) is bool
  if instantiate:
    return trace.instantiate_const(trace.full_raise(tracer))
  else:
    return tracer


FreeVar = namedtuple('FreeVar', ['val'])
ConstVar = namedtuple('ConstVar', ['val'])
LambdaBinding = namedtuple('LambdaBinding', [])
JaxprEqnRecipe = namedtuple('JaxprEqnRecipe',
                            ['eqn_id', 'invars', 'outvars', 'primitive', 'params'])

def new_eqn_recipe(invars, outvars, primitive, params):
  """Constructs a new JaxEqnRecipe.

  Params:
    invars: the tracers for the primitive inputs.
    outvars: the tracers for the primitive outputs.
    primitive: the primitive.
    params: the primitive params
  """
  if primitive.call_primitive:
    # TODO(necula): move these checks to core.check_jaxpr, and call it
    # in more places.
    assert "call_jaxpr" in params
  return JaxprEqnRecipe(object(), tuple(invars), map(ref, outvars), primitive,
                        params)


def recipe_to_eqn(unused_var, getvar, recipe):
  _, in_tracers, out_tracer_refs, primitive, params = recipe
  out_tracers = [t_ref() for t_ref in out_tracer_refs]
  invars  = [getvar(t) for t in in_tracers]
  outvars = [unused_var() if t is None else getvar(t) for t in out_tracers]
  return new_jaxpr_eqn(invars, outvars, primitive, params)

def tracers_to_jaxpr(in_tracers, out_tracers):
  """Constructs Jaxpr given tracers for inputs and outputs.

  Params:
    in_tracers: the tracers that were created for the function inputs
    out_tracers: the tracers that were output by the function.

  Returns: a triple of a `Jaxpr`, a list of constant values corresponding to
    the `constvars` in the returned Jaxps, and a list of environment values.
    The vars for the environment values have been pre-pended to the Jaxpr's
    `invars`.
  """
  newvar = core.gensym('')
  t_to_var = defaultdict(newvar)
  getvar = lambda t: t_to_var[id(t)]
  sorted_tracers = toposort(out_tracers)
  invars = map(getvar, in_tracers)
  eqns = []
  env = {}
  consts = {}
  const_to_var = defaultdict(newvar)
  processed_eqn_ids = set()
  for t in sorted_tracers:
    recipe = t.recipe
    if isinstance(recipe, JaxprEqnRecipe):
      if recipe.eqn_id not in processed_eqn_ids:
        eqns.append(recipe_to_eqn(newvar, getvar, recipe))
        processed_eqn_ids.add(recipe.eqn_id)
    elif isinstance(recipe, LambdaBinding):
      if all(t is not in_tracer for in_tracer in in_tracers):
        t._trace.escaped_tracer_error(f"Tracer not among input tracers {t}")
      assert in_tracers, "Lambda binding with no args"
    elif isinstance(recipe, FreeVar):
      env[getvar(t)] = recipe.val
    elif isinstance(recipe, ConstVar):
      v = t_to_var[id(t)] = const_to_var[id(recipe.val)]
      consts[v] = recipe.val
    elif isinstance(recipe, Literal):
      t_to_var[id(t)] = recipe
    elif recipe is unit:
      t_to_var[id(t)] = unitvar
    else:
      raise TypeError(recipe)

  env_vars, env_vals = unzip2(env.items())
  const_vars, const_vals = unzip2(consts.items())
  # The env_vars are pre-pended to the invars
  jaxpr = Jaxpr(const_vars, list(it.chain(env_vars, invars)), list(map(getvar, out_tracers)), eqns)
  core.skip_checks or core.check_jaxpr(jaxpr)
  return jaxpr, const_vals, env_vals

@cache()
def convert_constvars_jaxpr(jaxpr):
  """Moves the constvars to the start of invars."""
  core.skip_checks or core.check_jaxpr(jaxpr)
  lifted_jaxpr = Jaxpr(constvars=(),
                       invars=jaxpr.constvars + jaxpr.invars,
                       outvars=jaxpr.outvars, eqns=jaxpr.eqns)
  core.skip_checks or core.check_jaxpr(lifted_jaxpr)
  return lifted_jaxpr

def partial_eval_jaxpr(jaxpr, unknowns, instantiate):
  f = lu.wrap_init(core.jaxpr_as_fun(jaxpr))

  cell = []
  def fun(*vals):
    pvals = [PartialVal((aval, unit)) if uk else PartialVal((None, val))
             for aval, val, uk in zip(jaxpr.in_avals, vals, unknowns)]
    jaxpr_2, out_pvals_2, consts_2 = trace_to_jaxpr(f, pvals, instantiate=instantiate)
    out_pvs_2, out_consts_2 = unzip2(out_pvals_2)
    cell.append((out_pvs_2, jaxpr_2, len(consts_2)))
    return out_consts_2 + consts_2

  pvals = [PartialVal((abstract_unit, unit)) if uk else PartialVal((aval, unit))
           for aval, uk in zip(jaxpr.in_avals, unknowns)]
  jaxpr_1, out_pvals, consts_1 = trace_to_jaxpr(lu.wrap_init(fun), pvals, instantiate=True)
  (out_pvs_2, jaxpr_2, num_res), = cell
  assert len(jaxpr_2.constvars) == num_res

  #   jaxpr :: a -> b
  # jaxpr_1 :: a1 -> [b1, res]
  # jaxpr_2 :: res | a2 -> b2
  # jaxpr_2 :: [a2, res] -> b2
  jaxpr_2 = convert_constvars_jaxpr(jaxpr_2)
  jaxpr_2.invars = jaxpr_2.invars[num_res:] + jaxpr_2.invars[:num_res]
  uk_out = [pv is not None for pv in out_pvs_2]

  in_avals_1, in_avals_2 = unzip2(map(_split_aval, unknowns, jaxpr.in_avals))
  out_avals_1, out_avals_2 = unzip2(map(_split_aval, uk_out, jaxpr.out_avals))
  # out_avals_1 and in_avals_2 need the residuals added
  out_pvs, _ = unzip2(out_pvals)
  res_avals = out_pvs[len(jaxpr.out_avals):]
  assert len(res_avals) == num_res
  out_avals_1 = out_avals_1 + res_avals
  in_avals_2 = in_avals_2 + res_avals

  typed_jaxpr_1 = TypedJaxpr(jaxpr_1, consts_1, in_avals_1, out_avals_1)
  typed_jaxpr_2 = TypedJaxpr(jaxpr_2, (), in_avals_2, out_avals_2)
  return typed_jaxpr_1, typed_jaxpr_2, uk_out

def _split_aval(unknown, aval):
  return (abstract_unit, aval) if unknown else (aval, abstract_unit)


remat_call_p = core.Primitive('remat_call')
remat_call_p.call_primitive = True
remat_call = partial(core.call_bind, remat_call_p)
remat_call_p.def_custom_bind(remat_call)
remat_call_p.def_impl(core.call_impl)
remat_call_p.multiple_results = True

def _remat_partial_eval(trace, f, tracers, params):
  concrete = params['concrete']

  # Unlike JaxprTrace.process_call, we want to form a jaxpr for the entirety of
  # the function being called, not just for the unknown parts. To do that, we
  # instantiate all the input tracers as constants in the jaxpr being formed.
  # Those tracers might have concrete avals, and doing abstract interpretation
  # on concrete avals engenders a tradeoff: it allows data-dependent Python
  # control flow to work, but it can in some cases lead to redundant FLOPs (done
  # both in the `bind` call below and the `core.jaxpr_as_fun` call). We use the
  # `concrete` parameter to switch this behavior, and if `concrete` is False
  # then we raise the avals to the Shaped level.
  if concrete:
    instantiated_tracers = map(trace.instantiate_const, tracers)
  else:
    instantiated_tracers = map(trace.instantiate_const_abstracted, tracers)

  # Using the instantiated tracers, run call_bind like JaxprTrace.process_call.
  in_pvs, in_consts = unzip2(t.pval for t in instantiated_tracers)
  fun, aux = partial_eval(f, trace, in_pvs)
  if concrete:
    # TODO(mattjj): remove `remat_context` when confident no accidental FLOPs
    with remat_context():
      out_flat = remat_call_p.bind(fun, *in_consts, **params)
  else:
    out_flat = remat_call_p.bind(fun, *in_consts, **params)
  out_pvs, jaxpr, env = aux()
  env = map(trace.full_raise, env)
  out_pval_consts1, consts = split_list(out_flat, [len(out_flat)-len(jaxpr.constvars)])
  out_pvals1 = [PartialVal((pv, const)) for pv, const in zip(out_pvs, out_pval_consts1)]

  # Since we traced with everything marked as unknown, but we need to know which
  # outputs are known/unknown, we use partial_eval_jaxpr to get out_unknowns.

  in_avals = ([raise_to_shaped(partial_val_aval(*t.pval)) for t in env]
              + [raise_to_shaped(pv) for pv in in_pvs])
  out_avals = [raise_to_shaped(pv if pv is not None
                               else abstract_unit if var is unitvar
                               else get_aval(var.val) if type(var) is Literal
                               else get_aval(const))
               for var, pv, const in zip(jaxpr.outvars, out_pvs, out_pval_consts1)]
  typed_jaxpr = core.TypedJaxpr(jaxpr, consts, in_avals, out_avals)
  in_unknowns = [t.pval[0] is not None for t in it.chain(env, tracers)]
  jaxpr_1, jaxpr_2, out_unknowns = partial_eval_jaxpr(typed_jaxpr, in_unknowns, False)
  num_res = len(jaxpr_1.out_avals) - len(jaxpr_2.out_avals)

  # First, we prune the jaxpr to be staged out not to have too many outputs.
  typed_jaxpr = _dce_jaxpr(typed_jaxpr, out_unknowns)

  # Next, we need values for the outputs that should be known. Since consts
  # weren't passed through Python for evaluation, we need to evaluate jaxpr_1,
  # minus the residual outputs that we don't need. When `concrete=True`, as an
  # optimization we can avoid redoing *some* redundant FLOPs, namely those that
  # produced concrete avals at the output, simply by using those as computed
  # values. For the use case of reverse-mode ad in op-by-op ("eager mode")
  # evaluation, all the primal outputs should be concrete (thus not recomputed).
  to_compute = [not uk and type(pv) is not ConcreteArray
                for uk, pv in zip(out_unknowns, out_pvs)]
  jaxpr_1_primals = _dce_jaxpr(jaxpr_1, to_compute + [False] * num_res)
  _, in_consts = unzip2(t.pval for t in it.chain(env, tracers))
  out_pval_consts2 = core.jaxpr_as_fun(jaxpr_1_primals)(*in_consts)[:-num_res or None]
  out_pvals = map(_reconstruct_pval, out_pvals1, out_pval_consts2, out_unknowns)

  # Now that we have out_pvals, the rest is just like JaxprTrace.process_call.
  instantiated_tracers = env + instantiated_tracers
  const_tracers = map(trace.new_instantiated_const, consts)
  lifted_jaxpr = convert_constvars_jaxpr(typed_jaxpr.jaxpr)
  out_tracers = [JaxprTracer(trace, out_pval, None) for out_pval in out_pvals]
  new_params = dict(params, call_jaxpr=lifted_jaxpr)
  eqn = new_eqn_recipe(tuple(it.chain(const_tracers, instantiated_tracers)),
                       out_tracers, remat_call_p, new_params)
  for t in out_tracers: t.recipe = eqn
  return out_tracers
call_partial_eval_rules[remat_call_p] = _remat_partial_eval

def _dce_jaxpr(typed_jaxpr, outputs):
  # This dead-code elimination is pretty rudimentary, and in particular doesn't
  # nontrivially DCE through scan, call, or other higher-order primitives.
  # TODO(mattjj): better DCE
  jaxpr = typed_jaxpr.jaxpr
  outvars, out_avals = jaxpr.outvars, typed_jaxpr.out_avals
  out_pairs = [(var, aval) if output else (unitvar, core.abstract_unit)
              for var, aval, output in zip(outvars, out_avals, outputs)]
  new_outvars, new_out_avals = unzip2(out_pairs)

  needed_vars = {v for v in new_outvars if type(v) is not Literal}
  new_eqns = []
  for eqn in jaxpr.eqns[::-1]:
    if set(eqn.outvars) & needed_vars:
      new_eqns.append(eqn)
      needed_vars.update(v for v in eqn.invars if type(v) is not Literal)
  new_eqns = new_eqns[::-1]
  new_jaxpr = core.Jaxpr(jaxpr.constvars, jaxpr.invars,
                         new_outvars, new_eqns)
  return core.TypedJaxpr(new_jaxpr, typed_jaxpr.literals, typed_jaxpr.in_avals,
                         new_out_avals)

def _reconstruct_pval(pval1, const2, unknown):
  pv1, const1 = pval1
  if unknown or pv1 is None:
    return pval1
  if type(pv1) is ConcreteArray:
    return PartialVal((None, pv1.val))
  else:
    return PartialVal((None, const2))

# TODO(mattjj): for https://github.com/google/jax/pull/1749 we allowed
# standard_abstract_eval to perform concrete evaluation (i.e. FLOPs), but we
# don't think it should happen except for in a remat context
@contextlib.contextmanager
def remat_context():
  try:
    prev_state = _thread_local_state.remat
    _thread_local_state.remat = True
    yield
  finally:
    _thread_local_state.remat = prev_state

class _ThreadLocalState(threading.local):
  def __init__(self):
    self.remat = False
_thread_local_state = _ThreadLocalState()


def move_binders_to_front(typed_jaxpr, to_move):
  assert not typed_jaxpr.jaxpr.constvars
  assert len(typed_jaxpr.in_avals) == len(to_move)
  new_invars = _move_to_front(typed_jaxpr.jaxpr.invars, to_move)
  new_jaxpr = core.Jaxpr((), new_invars, typed_jaxpr.jaxpr.outvars,
                         typed_jaxpr.jaxpr.eqns)
  new_in_avals = _move_to_front(typed_jaxpr.in_avals, to_move)
  return core.TypedJaxpr(new_jaxpr, typed_jaxpr.literals, new_in_avals,
                         typed_jaxpr.out_avals)

def _move_to_front(lst, to_move):
  return ([elt for elt, move in zip(lst, to_move) if move] +
          [elt for elt, move in zip(lst, to_move) if not move])
