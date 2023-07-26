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


from functools import partial

import scipy.linalg
import textwrap

from jax import jit, vmap
from .. import lax
from .. import lax_linalg
from ..numpy.lax_numpy import _wraps
from ..numpy import lax_numpy as np
from ..numpy import linalg as np_linalg

_T = lambda x: np.swapaxes(x, -1, -2)

@partial(jit, static_argnums=(1,))
def _cholesky(a, lower):
  a = np_linalg._promote_arg_dtypes(np.asarray(a))
  l = lax_linalg.cholesky(a if lower else np.conj(_T(a)), symmetrize_input=False)
  return l if lower else np.conj(_T(l))

@_wraps(scipy.linalg.cholesky)
def cholesky(a, lower=False, overwrite_a=False, check_finite=True):
  del overwrite_a, check_finite
  return _cholesky(a, lower)

@_wraps(scipy.linalg.cho_factor)
def cho_factor(a, lower=False, overwrite_a=False, check_finite=True):
  return (cholesky(a, lower=lower), lower)

@partial(jit, static_argnums=(2,))
def _cho_solve(c, b, lower):
  c, b = np_linalg._promote_arg_dtypes(np.asarray(c), np.asarray(b))
  np_linalg._check_solve_shapes(c, b)
  # TODO(phawkins): triangular_solve only supports matrices on the RHS, so we
  # add a dummy dimension. Extend it to support vectors and simplify this.
  rhs_vector = c.ndim == b.ndim + 1
  b = b[..., np.newaxis] if rhs_vector else b
  b = lax_linalg.triangular_solve(c, b, left_side=True, lower=lower,
                                  transpose_a=not lower, conjugate_a=not lower)
  b = lax_linalg.triangular_solve(c, b, left_side=True, lower=lower,
                                  transpose_a=lower, conjugate_a=lower)
  return b[..., 0] if rhs_vector else b

@_wraps(scipy.linalg.cho_solve, update_doc=False)
def cho_solve(c_and_lower, b, overwrite_b=False, check_finite=True):
  del overwrite_b, check_finite
  c, lower = c_and_lower
  return _cho_solve(c, b, lower)

@_wraps(scipy.linalg.svd)
def svd(a, full_matrices=True, compute_uv=True, overwrite_a=False,
        check_finite=True, lapack_driver='gesdd'):
  del overwrite_a, check_finite, lapack_driver
  a = np_linalg._promote_arg_dtypes(np.asarray(a))
  return lax_linalg.svd(a, full_matrices, compute_uv)


@_wraps(scipy.linalg.det)
def det(a, overwrite_a=False, check_finite=True):
  del overwrite_a, check_finite
  return np_linalg.det(a)


@_wraps(scipy.linalg.eigh)
def eigh(a, b=None, lower=True, eigvals_only=False, overwrite_a=False,
         overwrite_b=False, turbo=True, eigvals=None, type=1,
         check_finite=True):
  del overwrite_a, overwrite_b, turbo, check_finite
  if b is not None:
    raise NotImplementedError("Only the b=None case of eigh is implemented")
  if type != 1:
    raise NotImplementedError("Only the type=1 case of eigh is implemented.")
  if eigvals is not None:
    raise NotImplementedError(
        "Only the eigvals=None case of eigh is implemented.")

  a = np_linalg._promote_arg_dtypes(np.asarray(a))
  v, w = lax_linalg.eigh(a, lower=lower)

  return w if eigvals_only else (w, v)


@_wraps(scipy.linalg.inv)
def inv(a, overwrite_a=False, check_finite=True):
  del overwrite_a, check_finite
  return np_linalg.inv(a)


@_wraps(scipy.linalg.lu_factor)
def lu_factor(a, overwrite_a=False, check_finite=True):
  del overwrite_a, check_finite
  a = np_linalg._promote_arg_dtypes(np.asarray(a))
  return lax_linalg.lu(a)


@_wraps(scipy.linalg.lu_solve)
def lu_solve(lu_and_piv, b, trans=0, overwrite_b=False, check_finite=True):
  del overwrite_b, check_finite
  lu, pivots = lu_and_piv
  return lax_linalg.lu_solve(lu, pivots, b, trans)


@partial(jit, static_argnums=(1,))
def _lu(a, permute_l):
  a = np_linalg._promote_arg_dtypes(np.asarray(a))
  lu, pivots = lax_linalg.lu(a)
  dtype = lax.dtype(a)
  m, n = np.shape(a)
  permutation = lax_linalg.lu_pivots_to_permutation(pivots, m)
  p = np.real(np.array(permutation == np.arange(m)[:, None], dtype=dtype))
  k = min(m, n)
  l = np.tril(lu, -1)[:, :k] + np.eye(m, k, dtype=dtype)
  u = np.triu(lu)[:k, :]
  return (np.matmul(p, l), u) if permute_l else (p, l, u)

@_wraps(scipy.linalg.lu, update_doc=False)
def lu(a, permute_l=False, overwrite_a=False, check_finite=True):
  del overwrite_a, check_finite
  return _lu(a, permute_l)

@partial(jit, static_argnums=(1, 2))
def _qr(a, mode, pivoting):
  if pivoting:
    raise NotImplementedError(
        "The pivoting=True case of qr is not implemented.")
  if mode in ("full", "r"):
    full_matrices = True
  elif mode == "economic":
    full_matrices = False
  else:
    raise ValueError(f"Unsupported QR decomposition mode '{mode}'")
  a = np_linalg._promote_arg_dtypes(np.asarray(a))
  q, r = lax_linalg.qr(a, full_matrices)
  return r if mode == "r" else (q, r)

@_wraps(scipy.linalg.qr)
def qr(a, overwrite_a=False, lwork=None, mode="full", pivoting=False,
       check_finite=True):
  del overwrite_a, lwork, check_finite
  return _qr(a, mode, pivoting)


@partial(jit, static_argnums=(2, 3))
def _solve(a, b, sym_pos, lower):
  if not sym_pos:
    return np_linalg.solve(a, b)

  a, b = np_linalg._promote_arg_dtypes(np.asarray(a), np.asarray(b))
  np_linalg._check_solve_shapes(a, b)

  # With custom_linear_solve, we can reuse the same factorization when
  # computing sensitivities. This is considerably faster.
  factors = lax.stop_gradient(cho_factor)(a, lower=lower)
  custom_solve = partial(
      lax.custom_linear_solve,
      lambda x: np_linalg._matvec_multiply(a, x),
      solve=lambda _, x: cho_solve(factors, x),
      symmetric=True)
  if a.ndim == b.ndim + 1:
    # b.shape == [..., m]
    return custom_solve(b)
  else:
    # b.shape == [..., m, k]
    return vmap(custom_solve, b.ndim - 1, max(a.ndim, b.ndim) - 1)(b)


@_wraps(scipy.linalg.solve)
def solve(a, b, sym_pos=False, lower=False, overwrite_a=False, overwrite_b=False,
          debug=False, check_finite=True):
  del overwrite_a, overwrite_b, debug, check_finite
  return _solve(a, b, sym_pos, lower)

@partial(jit, static_argnums=(2, 3, 4))
def _solve_triangular(a, b, trans, lower, unit_diagonal):
  if trans in [0, "N"]:
    transpose_a, conjugate_a = False, False
  elif trans in [1, "T"]:
    transpose_a, conjugate_a = True, False
  elif trans in [2, "C"]:
    transpose_a, conjugate_a = True, True
  else:
    raise ValueError(f"Invalid 'trans' value {trans}")

  a, b = np_linalg._promote_arg_dtypes(np.asarray(a), np.asarray(b))

  # lax_linalg.triangular_solve only supports matrix 'b's at the moment.
  b_is_vector = np.ndim(a) == np.ndim(b) + 1
  if b_is_vector:
    b = b[..., None]
  out = lax_linalg.triangular_solve(a, b, left_side=True, lower=lower,
                                    transpose_a=transpose_a,
                                    conjugate_a=conjugate_a,
                                    unit_diagonal=unit_diagonal)
  return out[..., 0] if b_is_vector else out

@_wraps(scipy.linalg.solve_triangular)
def solve_triangular(a, b, trans=0, lower=False, unit_diagonal=False,
                     overwrite_b=False, debug=None, check_finite=True):
  del overwrite_b, debug, check_finite
  return _solve_triangular(a, b, trans, lower, unit_diagonal)



@_wraps(scipy.linalg.tril)
def tril(m, k=0):
  return np.tril(m, k)


@_wraps(scipy.linalg.triu)
def triu(m, k=0):
  return np.triu(m, k)

@_wraps(scipy.linalg.expm, lax_description=textwrap.dedent("""\
    In addition to the original NumPy argument(s) listed below,
    also supports the optional boolean argument ``upper_triangular``
    to specify whether the ``A`` matrix is upper triangular.
    """))
def expm(A, *, upper_triangular=False):
    return _expm(A, upper_triangular)

def _expm(A, upper_triangular=False):
    P,Q,n_squarings = _calc_P_Q(A)
    R = _solve_P_Q(P, Q, upper_triangular)
    R = _squaring(R, n_squarings)
    return R

@jit
def _calc_P_Q(A):
  A = np.asarray(A)
  if A.ndim != 2 or A.shape[0] != A.shape[1]:
      raise ValueError('expected A to be a square matrix')
  A_L1 = np_linalg.norm(A,1)
  n_squarings = 0
  if A.dtype in ['float64', 'complex128']:
    U3,V3 = _pade3(A)
    U5,V5 = _pade5(A)
    U7,V7 = _pade7(A)
    U9,V9 = _pade9(A)
    maxnorm = 5.371920351148152
    n_squarings = np.maximum(0, np.floor(np.log2(A_L1 / maxnorm)))
    A = A / 2**n_squarings
    U13,V13 = _pade13(A)
    conds=np.array([1.495585217958292e-002, 2.539398330063230e-001, 9.504178996162932e-001, 2.097847961257068e+000])
    U = np.select((maxnorm<conds),(U3,U5,U7,U9),U13)
    V = np.select((maxnorm<conds),(V3,V5,V7,V9),V13)
  elif A.dtype in ['float32', 'complex64']:
    U3,V3 = _pade3(A)
    U5,V5 = _pade5(A)
    maxnorm = 3.925724783138660
    n_squarings = np.maximum(0, np.floor(np.log2(A_L1 / maxnorm)))
    A = A / 2**n_squarings
    U7,V7 = _pade7(A)
    conds=np.array([4.258730016922831e-001, 1.880152677804762e+000])
    U = np.select((maxnorm<conds),(U3,U5),U7)
    V = np.select((maxnorm<conds),(V3,V5),V7)
  else:
    raise TypeError(f"A.dtype={A.dtype} is not supported.")
  P = U + V  # p_m(A) : numerator
  Q = -U + V # q_m(A) : denominator
  return P,Q,n_squarings

def _solve_P_Q(P, Q, upper_triangular=False):
  return solve_triangular(Q, P) if upper_triangular else np_linalg.solve(Q,P)

@jit
def _squaring(R, n_squarings):
    # squaring step to undo scaling
    def my_body_fun(i,R):
      return np.dot(R,R)
    lower = np.zeros(1, dtype=n_squarings.dtype)
    R = lax.fori_loop(lower[0],n_squarings,my_body_fun,R)
    return R

def _pade3(A):
    b = (120., 60., 12., 1.)
    ident = np.eye(*A.shape, dtype=A.dtype)
    A2 = np.dot(A,A)
    U = np.dot(A , (b[3]*A2 + b[1]*ident))
    V = b[2]*A2 + b[0]*ident
    return U,V

def _pade5(A):
    b = (30240., 15120., 3360., 420., 30., 1.)
    ident = np.eye(*A.shape, dtype=A.dtype)
    A2 = np.dot(A,A)
    A4 = np.dot(A2,A2)
    U = np.dot(A, b[5]*A4 + b[3]*A2 + b[1]*ident)
    V = b[4]*A4 + b[2]*A2 + b[0]*ident
    return U,V

def _pade7(A):
    b = (17297280., 8648640., 1995840., 277200., 25200., 1512., 56., 1.)
    ident = np.eye(*A.shape, dtype=A.dtype)
    A2 = np.dot(A,A)
    A4 = np.dot(A2,A2)
    A6 = np.dot(A4,A2)
    U = np.dot(A, b[7]*A6 + b[5]*A4 + b[3]*A2 + b[1]*ident)
    V = b[6]*A6 + b[4]*A4 + b[2]*A2 + b[0]*ident
    return U,V

def _pade9(A):
    b = (17643225600., 8821612800., 2075673600., 302702400., 30270240.,
                2162160., 110880., 3960., 90., 1.)
    ident = np.eye(*A.shape, dtype=A.dtype)
    A2 = np.dot(A,A)
    A4 = np.dot(A2,A2)
    A6 = np.dot(A4,A2)
    A8 = np.dot(A6,A2)
    U = np.dot(A, b[9]*A8 + b[7]*A6 + b[5]*A4 + b[3]*A2 + b[1]*ident)
    V = b[8]*A8 + b[6]*A6 + b[4]*A4 + b[2]*A2 + b[0]*ident
    return U,V

def _pade13(A):
    b = (64764752532480000., 32382376266240000., 7771770303897600.,
    1187353796428800., 129060195264000., 10559470521600., 670442572800.,
    33522128640., 1323241920., 40840800., 960960., 16380., 182., 1.)
    ident = np.eye(*A.shape, dtype=A.dtype)
    A2 = np.dot(A,A)
    A4 = np.dot(A2,A2)
    A6 = np.dot(A4,A2)
    U = np.dot(A,np.dot(A6, b[13]*A6 + b[11]*A4 + b[9]*A2) + b[7]*A6 + b[5]*A4 + b[3]*A2 + b[1]*ident)
    V = np.dot(A6, b[12]*A6 + b[10]*A4 + b[8]*A2) + b[6]*A6 + b[4]*A4 + b[2]*A2 + b[0]*ident
    return U,V


@_wraps(scipy.linalg.block_diag)
@jit
def block_diag(*arrs):
  if not arrs:
    arrs = [np.zeros((1, 0))]
  arrs = np._promote_dtypes(*arrs)
  if bad_shapes := [i for i, a in enumerate(arrs) if np.ndim(a) > 2]:
    raise ValueError(
        f"Arguments to jax.scipy.linalg.block_diag must have at most 2 dimensions, got {arrs[bad_shapes[0]]} at argument {bad_shapes[0]}."
    )
  arrs = [np.atleast_2d(a) for a in arrs]
  acc = arrs[0]
  dtype = lax.dtype(acc)
  for a in arrs[1:]:
    _, c = a.shape
    a = lax.pad(a, dtype.type(0), ((0, 0, 0), (acc.shape[-1], 0, 0)))
    acc = lax.pad(acc, dtype.type(0), ((0, 0, 0), (0, c, 0)))
    acc = lax.concatenate([acc, a], dimension=0)
  return acc
