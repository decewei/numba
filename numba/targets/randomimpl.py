"""
Implement the random and numpy.random module functions.
"""

from __future__ import print_function, absolute_import, division

import math
import random

import numpy as np

from llvmlite import ir

from numba.targets.imputils import implement, Registry
from numba.typing import signature
from numba import _helperlib, cgutils, errcode, types, utils


registry = Registry()
register = registry.register

int32_t = ir.IntType(32)
int64_t = ir.IntType(64)
def const_int(x):
    return ir.Constant(int32_t, x)
double = ir.DoubleType()

N = 624
N_const = ir.Constant(int32_t, N)

# This is the same struct as rnd_state_t in _helperlib.c.
rnd_state_t = ir.LiteralStructType(
    [int32_t, ir.ArrayType(int32_t, N),
     int32_t, double])
rnd_state_ptr_t = ir.PointerType(rnd_state_t)

# Accessors
def get_index_ptr(builder, state_ptr):
    return cgutils.gep(builder, state_ptr, 0, 0)

def get_array_ptr(builder, state_ptr):
    return cgutils.gep(builder, state_ptr, 0, 1)

def get_has_gauss_ptr(builder, state_ptr):
    return cgutils.gep(builder, state_ptr, 0, 2)

def get_gauss_ptr(builder, state_ptr):
    return cgutils.gep(builder, state_ptr, 0, 3)


def get_next_int32(context, builder, state_ptr):
    """
    Get the next int32 generated by the PRNG at *state_ptr*.
    """
    idxptr = get_index_ptr(builder, state_ptr)
    idx = builder.load(idxptr)
    need_reshuffle = builder.icmp_unsigned('>=', idx, N_const)
    with cgutils.if_unlikely(builder, need_reshuffle):
        fnty = ir.FunctionType(ir.VoidType(), (rnd_state_ptr_t,))
        fn = builder.function.module.get_or_insert_function(fnty, "numba_rnd_shuffle")
        builder.call(fn, (state_ptr,))
        builder.store(const_int(0), idxptr)
    idx = builder.load(idxptr)
    array_ptr = get_array_ptr(builder, state_ptr)
    y = builder.load(cgutils.gep(builder, array_ptr, 0, idx))
    idx = builder.add(idx, const_int(1))
    builder.store(idx, idxptr)
    # Tempering
    y = builder.xor(y, builder.lshr(y, const_int(11)))
    y = builder.xor(y, builder.and_(builder.shl(y, const_int(7)),
                                    const_int(0x9d2c5680)))
    y = builder.xor(y, builder.and_(builder.shl(y, const_int(15)),
                                    const_int(0xefc60000)))
    y = builder.xor(y, builder.lshr(y, const_int(18)))
    return y

def get_next_double(context, builder, state_ptr):
    """
    Get the next double generated by the PRNG at *state_ptr*.
    """
    # a = rk_random(state) >> 5, b = rk_random(state) >> 6;
    a = builder.lshr(get_next_int32(context, builder, state_ptr), const_int(5))
    b = builder.lshr(get_next_int32(context, builder, state_ptr), const_int(6))

    # return (a * 67108864.0 + b) / 9007199254740992.0;
    a = builder.uitofp(a, double)
    b = builder.uitofp(b, double)
    return builder.fdiv(
        builder.fadd(b, builder.fmul(a, ir.Constant(double, 67108864.0))),
        ir.Constant(double, 9007199254740992.0))

def get_next_int(context, builder, state_ptr, nbits):
    """
    Get the next integer with width *nbits*.
    """
    c32 = ir.Constant(nbits.type, 32)
    def get_shifted_int(nbits):
        shift = builder.sub(c32, nbits)
        y = get_next_int32(context, builder, state_ptr)
        return builder.lshr(y, builder.zext(shift, y.type))

    ret = cgutils.alloca_once_value(builder, ir.Constant(int64_t, 0))

    is_32b = builder.icmp_unsigned('<=', nbits, c32)
    with cgutils.ifelse(builder, is_32b) as (ifsmall, iflarge):
        with ifsmall:
            low = get_shifted_int(nbits)
            builder.store(builder.zext(low, int64_t), ret)
        with iflarge:
            # XXX This assumes nbits <= 64
            low = get_next_int32(context, builder, state_ptr)
            high = get_shifted_int(builder.sub(nbits, c32))
            total = builder.add(
                builder.zext(low, int64_t),
                builder.shl(builder.zext(high, int64_t), ir.Constant(int64_t, 32)))
            builder.store(total, ret)

    return builder.load(ret)


def get_py_state_ptr(context, builder):
    return context.get_c_value(builder, rnd_state_t,
                               "numba_py_random_state")

def get_np_state_ptr(context, builder):
    return context.get_c_value(builder, rnd_state_t,
                               "numba_np_random_state")

def get_state_ptr(context, builder, name):
    return {
        "py": get_py_state_ptr,
        "np": get_np_state_ptr,
        }[name](context, builder)


def _fill_defaults(context, builder, sig, args, defaults):
    """
    Assuming a homogenous signature (same type for result and all arguments),
    fill in the *defaults* if missing from the arguments.
    """
    ty = sig.return_type
    llty = context.get_data_type(ty)
    args = args + [ir.Constant(llty, d) for d in defaults[len(args):]]
    sig = signature(*(ty,) * (len(args) + 1))
    return sig, args


@register
@implement("random.seed", types.uint32)
def seed_impl(context, builder, sig, args):
    return _seed_impl(context, builder, sig, args, get_state_ptr(context, builder, "py"))

@register
@implement("np.random.seed", types.uint32)
def seed_impl(context, builder, sig, args):
    return _seed_impl(context, builder, sig, args, get_state_ptr(context, builder, "np"))

def _seed_impl(context, builder, sig, args, state_ptr):
    seed_value, = args
    fnty = ir.FunctionType(ir.VoidType(), (rnd_state_ptr_t, int32_t))
    fn = builder.function.module.get_or_insert_function(fnty, "numba_rnd_init")
    builder.call(fn, (state_ptr, seed_value))
    return context.get_constant(types.none, None)

@register
@implement("random.random")
def random_impl(context, builder, sig, args):
    state_ptr = get_state_ptr(context, builder, "py")
    return get_next_double(context, builder, state_ptr)

@register
@implement("np.random.rand")
@implement("np.random.random")
def random_impl(context, builder, sig, args):
    state_ptr = get_state_ptr(context, builder, "np")
    return get_next_double(context, builder, state_ptr)


@register
@implement("random.gauss", types.Kind(types.Float), types.Kind(types.Float))
@implement("random.normalvariate", types.Kind(types.Float), types.Kind(types.Float))
def gauss_impl(context, builder, sig, args):
    return _gauss_impl(context, builder, sig, args, "py")


@register
@implement("np.random.randn")
@implement("np.random.standard_normal")
@implement("np.random.normal")
@implement("np.random.normal", types.Kind(types.Float))
@implement("np.random.normal", types.Kind(types.Float), types.Kind(types.Float))
def np_gauss_impl(context, builder, sig, args):
    sig, args = _fill_defaults(context, builder, sig, args, (0.0, 1.0))
    return _gauss_impl(context, builder, sig, args, "np")


def _gauss_pair_impl(_random):
    def compute_gauss_pair():
        """
        Compute a pair of numbers on the normal distribution.
        """
        while True:
            x1 = 2.0 * _random() - 1.0
            x2 = 2.0 * _random() - 1.0
            r2 = x1*x1 + x2*x2
            if r2 < 1.0 and r2 != 0.0:
                break

        # Box-Muller transform
        f = math.sqrt(-2.0 * math.log(r2) / r2)
        return f * x1, f * x2
    return compute_gauss_pair

def _gauss_impl(context, builder, sig, args, state):
    # The type for all computations (either float or double)
    ty = sig.return_type
    llty = context.get_data_type(ty)

    state_ptr = get_state_ptr(context, builder, state)
    _random = {"py": random.random,
               "np": np.random.random}[state]

    ret = cgutils.alloca_once(builder, llty, name="result")

    gauss_ptr = get_gauss_ptr(builder, state_ptr)
    has_gauss_ptr = get_has_gauss_ptr(builder, state_ptr)
    has_gauss = cgutils.is_true(builder, builder.load(has_gauss_ptr))
    with cgutils.ifelse(builder, has_gauss) as (then, otherwise):
        with then:
            # if has_gauss: return it
            builder.store(builder.load(gauss_ptr), ret)
            builder.store(const_int(0), has_gauss_ptr)
        with otherwise:
            # if not has_gauss: compute a pair of numbers using the Box-Muller
            # transform; keep one and return the other
            pair = context.compile_internal(builder,
                                            _gauss_pair_impl(_random),
                                            signature(types.UniTuple(ty, 2)),
                                            ())

            first, second = cgutils.unpack_tuple(builder, pair, 2)
            builder.store(first, gauss_ptr)
            builder.store(second, ret)
            builder.store(const_int(1), has_gauss_ptr)

    mu, sigma = args
    return builder.fadd(mu,
                        builder.fmul(sigma, builder.load(ret)))

@register
@implement("random.getrandbits", types.Kind(types.Integer))
def getrandbits_impl(context, builder, sig, args):
    nbits, = args
    state_ptr = get_state_ptr(context, builder, "py")
    return get_next_int(context, builder, state_ptr, nbits)


def _randrange_impl(context, builder, start, stop, step, state):
    state_ptr = get_state_ptr(context, builder, state)
    ty = stop.type
    zero = ir.Constant(ty, 0)
    one = ir.Constant(ty, 1)
    nptr = cgutils.alloca_once(builder, ty, name="n")
    # n = stop - start
    builder.store(builder.sub(stop, start), nptr)

    with cgutils.ifthen(builder, builder.icmp_signed('<', step, zero)):
        # n = (n + step + 1) // step
        w = builder.add(builder.add(builder.load(nptr), step), one)
        n = builder.sdiv(w, step)
        builder.store(n, nptr)
    with cgutils.ifthen(builder, builder.icmp_signed('>', step, one)):
        # n = (n + step - 1) // step
        w = builder.sub(builder.add(builder.load(nptr), step), one)
        n = builder.sdiv(w, step)
        builder.store(n, nptr)

    n = builder.load(nptr)
    with cgutils.if_unlikely(builder, builder.icmp_signed('<=', n, zero)):
        # n <= 0 => ValueError
        context.return_errcode(builder, errcode.RUNTIME_ERROR)

    fnty = ir.FunctionType(ty, [ty, cgutils.true_bit.type])
    fn = builder.function.module.get_or_insert_function(fnty, "llvm.ctlz.%s" % ty)
    nbits = builder.trunc(builder.call(fn, [n, cgutils.true_bit]), int32_t)
    nbits = builder.sub(ir.Constant(int32_t, ty.width), nbits)

    bbwhile = cgutils.append_basic_block(builder, "while")
    bbend = cgutils.append_basic_block(builder, "while.end")
    builder.branch(bbwhile)

    builder.position_at_end(bbwhile)
    r = get_next_int(context, builder, state_ptr, nbits)
    r = builder.trunc(r, ty)
    too_large = builder.icmp_signed('>=', r, n)
    builder.cbranch(too_large, bbwhile, bbend)

    builder.position_at_end(bbend)
    return builder.add(start, builder.mul(r, step))


@register
@implement("random.randrange", types.Kind(types.Integer))
def randrange_impl_1(context, builder, sig, args):
    stop, = args
    start = ir.Constant(stop.type, 0)
    step = ir.Constant(stop.type, 1)
    return _randrange_impl(context, builder, start, stop, step, "py")

@register
@implement("random.randrange", types.Kind(types.Integer), types.Kind(types.Integer))
def randrange_impl_2(context, builder, sig, args):
    start, stop = args
    step = ir.Constant(start.type, 1)
    return _randrange_impl(context, builder, start, stop, step, "py")

@register
@implement("random.randrange", types.Kind(types.Integer),
           types.Kind(types.Integer), types.Kind(types.Integer))
def randrange_impl_3(context, builder, sig, args):
    start, stop, step = args
    return _randrange_impl(context, builder, start, stop, step, "py")

@register
@implement("random.randint", types.Kind(types.Integer), types.Kind(types.Integer))
def randint_impl_1(context, builder, sig, args):
    start, stop = args
    step = ir.Constant(start.type, 1)
    stop = builder.add(stop, step)
    return _randrange_impl(context, builder, start, stop, step, "py")

@register
@implement("np.random.randint", types.Kind(types.Integer))
def randint_impl_2(context, builder, sig, args):
    stop, = args
    start = ir.Constant(stop.type, 0)
    step = ir.Constant(stop.type, 1)
    return _randrange_impl(context, builder, start, stop, step, "np")

@register
@implement("np.random.randint", types.Kind(types.Integer), types.Kind(types.Integer))
def randrange_impl_2(context, builder, sig, args):
    start, stop = args
    step = ir.Constant(start.type, 1)
    return _randrange_impl(context, builder, start, stop, step, "np")

@register
@implement("random.uniform", types.Kind(types.Float), types.Kind(types.Float))
def uniform_impl(context, builder, sig, args):
    return uniform_impl(context, builder, sig, args, "py")

@register
@implement("np.random.uniform", types.Kind(types.Float), types.Kind(types.Float))
def uniform_impl(context, builder, sig, args):
    return uniform_impl(context, builder, sig, args, "np")

def uniform_impl(context, builder, sig, args, state):
    state_ptr = get_state_ptr(context, builder, state)
    a, b = args
    width = builder.fsub(b, a)
    r = get_next_double(context, builder, state_ptr)
    return builder.fadd(a, builder.fmul(width, r))

@register
@implement("random.triangular", types.Kind(types.Float), types.Kind(types.Float))
def triangular_impl_2(context, builder, sig, args):
    fltty = sig.return_type
    low, high = args
    state_ptr = get_state_ptr(context, builder, "py")
    randval = get_next_double(context, builder, state_ptr)

    def triangular_impl_2(randval, low, high):
        u = randval
        c = 0.5
        if u > c:
            u = 1.0 - u
            low, high = high, low
        return low + (high - low) * math.sqrt(u * c)

    return context.compile_internal(builder, triangular_impl_2,
                                    signature(*(fltty,) * 4),
                                    (randval, low, high))

@register
@implement("random.triangular", types.Kind(types.Float),
           types.Kind(types.Float), types.Kind(types.Float))
def triangular_impl_3(context, builder, sig, args):
    low, high, mode = args
    return _triangular_impl_3(context, builder, sig, low, high, mode, "py")

@register
@implement("np.random.triangular", types.Kind(types.Float),
           types.Kind(types.Float), types.Kind(types.Float))
def triangular_impl_3(context, builder, sig, args):
    low, mode, high = args
    return _triangular_impl_3(context, builder, sig, low, high, mode, "np")

def _triangular_impl_3(context, builder, sig, low, high, mode, state):
    fltty = sig.return_type
    state_ptr = get_state_ptr(context, builder, state)
    randval = get_next_double(context, builder, state_ptr)

    def triangular_impl_3(randval, low, high, mode):
        if high == low:
            return low
        u = randval
        c = (mode - low) / (high - low)
        if u > c:
            u = 1.0 - u
            c = 1.0 - c
            low, high = high, low
        return low + (high - low) * math.sqrt(u * c)

    return context.compile_internal(builder, triangular_impl_3,
                                    signature(*(fltty,) * 5),
                                    (randval, low, high, mode))


@register
@implement("random.gammavariate",
           types.Kind(types.Float), types.Kind(types.Float))
def gammavariate_impl(context, builder, sig, args):
    return _gammavariate_impl(context, builder, sig, args, random.random)

@register
@implement("np.random.standard_gamma", types.Kind(types.Float))
@implement("np.random.gamma", types.Kind(types.Float))
@implement("np.random.gamma", types.Kind(types.Float), types.Kind(types.Float))
def gammavariate_impl(context, builder, sig, args):
    sig, args = _fill_defaults(context, builder, sig, args, (None, 1.0))
    return _gammavariate_impl(context, builder, sig, args, np.random.random)

def _gammavariate_impl(context, builder, sig, args, _random):
    _exp = math.exp
    _log = math.log
    _sqrt = math.sqrt
    _e = math.e

    TWOPI = 2.0 * math.pi
    LOG4 = _log(4.0)
    SG_MAGICCONST = 1.0 + _log(4.5)

    def gammavariate_impl(alpha, beta):
        """Gamma distribution.  Taken from CPython.
        """
        # alpha > 0, beta > 0, mean is alpha*beta, variance is alpha*beta**2

        # Warning: a few older sources define the gamma distribution in terms
        # of alpha > -1.0
        if alpha <= 0.0 or beta <= 0.0:
            # XXX error propagation doesn't work for user exceptions.
            # This will instead produce an "unknown error in native function".
            raise ValueError#('gammavariate: alpha and beta must be > 0.0')

        if alpha > 1.0:
            # Uses R.C.H. Cheng, "The generation of Gamma
            # variables with non-integral shape parameters",
            # Applied Statistics, (1977), 26, No. 1, p71-74
            ainv = _sqrt(2.0 * alpha - 1.0)
            bbb = alpha - LOG4
            ccc = alpha + ainv

            while 1:
                u1 = _random()
                if not 1e-7 < u1 < .9999999:
                    continue
                u2 = 1.0 - _random()
                v = _log(u1/(1.0-u1))/ainv
                x = alpha*_exp(v)
                z = u1*u1*u2
                r = bbb+ccc*v-x
                if r + SG_MAGICCONST - 4.5*z >= 0.0 or r >= _log(z):
                    return x * beta

        elif alpha == 1.0:
            # expovariate(1)
            u = _random()
            while u <= 1e-7:
                u = _random()
            return -_log(u) * beta

        else:   # alpha is between 0 and 1 (exclusive)
            # Uses ALGORITHM GS of Statistical Computing - Kennedy & Gentle
            while 1:
                u = _random()
                b = (_e + alpha)/_e
                p = b*u
                if p <= 1.0:
                    x = p ** (1.0/alpha)
                else:
                    x = -_log((b-p)/alpha)
                u1 = _random()
                if p > 1.0:
                    if u1 <= x ** (alpha - 1.0):
                        break
                elif u1 <= _exp(-x):
                    break
            return x * beta

    return context.compile_internal(builder, gammavariate_impl,
                                    sig, args)


@register
@implement("random.betavariate",
           types.Kind(types.Float), types.Kind(types.Float))
def betavariate_impl(context, builder, sig, args):
    return _betavariate_impl(context, builder, sig, args,
                             random.gammavariate)

@register
@implement("np.random.beta",
           types.Kind(types.Float), types.Kind(types.Float))
def betavariate_impl(context, builder, sig, args):
    return _betavariate_impl(context, builder, sig, args,
                             np.random.gamma)

def _betavariate_impl(context, builder, sig, args, gamma):

    def betavariate_impl(alpha, beta):
        """Beta distribution.  Taken from CPython.
        """
        # This version due to Janne Sinkkonen, and matches all the std
        # texts (e.g., Knuth Vol 2 Ed 3 pg 134 "the beta distribution").
        y = gamma(alpha, 1.)
        if y == 0.0:
            return 0.0
        else:
            return y / (y + gamma(beta, 1.))

    return context.compile_internal(builder, betavariate_impl,
                                    sig, args)


@register
@implement("random.expovariate",
           types.Kind(types.Float))
def expovariate_impl(context, builder, sig, args):
    _random = random.random
    _log = math.log

    def expovariate_impl(lambd):
        """Exponential distribution.  Taken from CPython.
        """
        # lambd: rate lambd = 1/mean
        # ('lambda' is a Python reserved word)

        # we use 1-random() instead of random() to preclude the
        # possibility of taking the log of zero.
        return -_log(1.0 - _random()) / lambd

    return context.compile_internal(builder, expovariate_impl,
                                    sig, args)


@register
@implement("np.random.exponential", types.Kind(types.Float))
def exponential_impl(context, builder, sig, args):
    _random = np.random.random
    _log = math.log

    def exponential_impl(scale):
        return -_log(1.0 - _random()) * scale

    return context.compile_internal(builder, exponential_impl,
                                    sig, args)

@register
@implement("np.random.standard_exponential")
@implement("np.random.exponential")
def exponential_impl(context, builder, sig, args):
    _random = np.random.random
    _log = math.log

    def exponential_impl():
        return -_log(1.0 - _random())

    return context.compile_internal(builder, exponential_impl,
                                    sig, args)


@register
@implement("np.random.lognormal")
@implement("np.random.lognormal", types.Kind(types.Float))
@implement("np.random.lognormal", types.Kind(types.Float), types.Kind(types.Float))
def np_lognormal_impl(context, builder, sig, args):
    sig, args = _fill_defaults(context, builder, sig, args, (0.0, 1.0))
    return _lognormvariate_impl(context, builder, sig, args,
                                np.random.normal)

@register
@implement("random.lognormvariate",
           types.Kind(types.Float), types.Kind(types.Float))
def lognormvariate_impl(context, builder, sig, args):
    return _lognormvariate_impl(context, builder, sig, args, random.gauss)

def _lognormvariate_impl(context, builder, sig, args, _gauss):
    _exp = math.exp

    def lognormvariate_impl(mu, sigma):
        return _exp(_gauss(mu, sigma))

    return context.compile_internal(builder, lognormvariate_impl,
                                    sig, args)


@register
@implement("random.paretovariate", types.Kind(types.Float))
def paretovariate_impl(context, builder, sig, args):
    _random = random.random

    def paretovariate_impl(alpha):
        """Pareto distribution.  Taken from CPython."""
        # Jain, pg. 495
        u = 1.0 - _random()
        return 1.0 / u ** (1.0/alpha)

    return context.compile_internal(builder, paretovariate_impl,
                                    sig, args)

@register
@implement("np.random.pareto", types.Kind(types.Float))
def pareto_impl(context, builder, sig, args):
    _random = np.random.random

    def pareto_impl(alpha):
        # Same as paretovariate() - 1.
        u = 1.0 - _random()
        return 1.0 / u ** (1.0/alpha) - 1

    return context.compile_internal(builder, pareto_impl, sig, args)


@register
@implement("random.weibullvariate",
           types.Kind(types.Float), types.Kind(types.Float))
def weibullvariate_impl(context, builder, sig, args):
    _random = random.random
    _log = math.log

    def weibullvariate_impl(alpha, beta):
        """Weibull distribution.  Taken from CPython."""
        # Jain, pg. 499; bug fix courtesy Bill Arms
        u = 1.0 - _random()
        return alpha * (-_log(u)) ** (1.0/beta)

    return context.compile_internal(builder, weibullvariate_impl,
                                    sig, args)

@register
@implement("np.random.weibull", types.Kind(types.Float))
def weibull_impl(context, builder, sig, args):
    _random = np.random.random
    _log = math.log

    def weibull_impl(beta):
        # Same as weibullvariate(1.0, beta)
        u = 1.0 - _random()
        return (-_log(u)) ** (1.0/beta)

    return context.compile_internal(builder, weibull_impl, sig, args)


@register
@implement("random.vonmisesvariate",
           types.Kind(types.Float), types.Kind(types.Float))
def vonmisesvariate_impl(context, builder, sig, args):
    return _vonmisesvariate_impl(context, builder, sig, args, random.random)

@register
@implement("np.random.vonmises",
           types.Kind(types.Float), types.Kind(types.Float))
def vonmisesvariate_impl(context, builder, sig, args):
    return _vonmisesvariate_impl(context, builder, sig, args, np.random.random)

def _vonmisesvariate_impl(context, builder, sig, args, _random):
    _exp = math.exp
    _sqrt = math.sqrt
    _cos = math.cos
    _acos = math.acos
    _pi = math.pi
    TWOPI = 2.0 * _pi

    def vonmisesvariate_impl(mu, kappa):
        """Circular data distribution.  Taken from CPython.
        Note the algorithm in Python 2.6 and Numpy is different:
        http://bugs.python.org/issue17141
        """
        # mu:    mean angle (in radians between 0 and 2*pi)
        # kappa: concentration parameter kappa (>= 0)
        # if kappa = 0 generate uniform random angle

        # Based upon an algorithm published in: Fisher, N.I.,
        # "Statistical Analysis of Circular Data", Cambridge
        # University Press, 1993.

        # Thanks to Magnus Kessler for a correction to the
        # implementation of step 4.
        if kappa <= 1e-6:
            return TWOPI * _random()

        s = 0.5 / kappa
        r = s + _sqrt(1.0 + s * s)

        while 1:
            u1 = _random()
            z = _cos(_pi * u1)

            d = z / (r + z)
            u2 = _random()
            if u2 < 1.0 - d * d or u2 <= (1.0 - d) * _exp(d):
                break

        q = 1.0 / r
        f = (q + z) / (1.0 + q * z)
        u3 = _random()
        if u3 > 0.5:
            theta = (mu + _acos(f)) % TWOPI
        else:
            theta = (mu - _acos(f)) % TWOPI

        return theta

    return context.compile_internal(builder, vonmisesvariate_impl,
                                    sig, args)


@register
@implement("np.random.chisquare", types.Kind(types.Float))
def chisquare_impl(context, builder, sig, args):

    def chisquare_impl(df):
        return 2.0 * np.random.standard_gamma(df / 2.0)

    return context.compile_internal(builder, chisquare_impl, sig, args)


@register
@implement("np.random.f", types.Kind(types.Float), types.Kind(types.Float))
def f_impl(context, builder, sig, args):

    def f_impl(num, denom):
        return ((np.random.chisquare(num) * denom) /
                (np.random.chisquare(denom) * num))

    return context.compile_internal(builder, f_impl, sig, args)


@register
@implement("np.random.geometric", types.Kind(types.Float))
def geometric_impl(context, builder, sig, args):
    _random = np.random.random
    intty = sig.return_type

    def geometric_impl(p):
        # Numpy's algorithm.
        if p <= 0.0 or p > 1.0:
            raise ValueError
        q = 1.0 - p
        if p >= 0.333333333333333333333333:
            X = intty(1)
            sum = prod = p
            U = _random()
            while U > sum:
                prod *= q
                sum += prod
                X += 1
            return X
        else:
            return math.ceil(math.log(1.0 - _random()) / math.log(q))

    return context.compile_internal(builder, geometric_impl, sig, args)


@register
@implement("np.random.gumbel", types.Kind(types.Float), types.Kind(types.Float))
def gumbel_impl(context, builder, sig, args):
    _random = np.random.random
    _log = math.log

    def gumbel_impl(loc, scale):
        U = 1.0 - _random()
        return loc - scale * _log(-_log(U))

    return context.compile_internal(builder, gumbel_impl, sig, args)


@register
@implement("np.random.hypergeometric", types.Kind(types.Integer),
           types.Kind(types.Integer), types.Kind(types.Integer))
def hypergeometric_impl(context, builder, sig, args):
    _random = np.random.random
    _floor = math.floor

    def hypergeometric_impl(ngood, nbad, nsamples):
        """Numpy's algorithm for hypergeometric()."""
        d1 = nbad + ngood - nsamples
        d2 = float(min(nbad, ngood))

        Y = d2
        K = nsamples
        while Y > 0.0 and K > 0:
            Y -= _floor(_random() + Y / (d1 + K))
            K -= 1
        Z = int(d2 - Y)
        if ngood > nbad:
            return nsamples - Z
        else:
            return Z

    return context.compile_internal(builder, hypergeometric_impl, sig, args)


@register
@implement("np.random.laplace")
@implement("np.random.laplace", types.Kind(types.Float))
@implement("np.random.laplace", types.Kind(types.Float), types.Kind(types.Float))
def laplace_impl(context, builder, sig, args):
    _random = np.random.random
    _log = math.log

    def laplace_impl(loc, scale):
        U = _random()
        if U < 0.5:
            return loc + scale * _log(U + U)
        else:
            return loc - scale * _log(2.0 - U - U)

    sig, args = _fill_defaults(context, builder, sig, args, (0.0, 1.0))
    return context.compile_internal(builder, laplace_impl, sig, args)


@register
@implement("np.random.logistic")
@implement("np.random.logistic", types.Kind(types.Float))
@implement("np.random.logistic", types.Kind(types.Float), types.Kind(types.Float))
def logistic_impl(context, builder, sig, args):
    _random = np.random.random
    _log = math.log

    def logistic_impl(loc, scale):
        U = _random()
        return loc + scale * _log(U / (1.0 - U))

    sig, args = _fill_defaults(context, builder, sig, args, (0.0, 1.0))
    return context.compile_internal(builder, logistic_impl, sig, args)

@register
@implement("np.random.logseries", types.Kind(types.Float))
def logseries_impl(context, builder, sig, args):
    intty = sig.return_type
    _random = np.random.random
    _log = math.log
    _exp = math.exp

    def logseries_impl(p):
        """Numpy's algorithm for logseries()."""
        if p <= 0.0 or p > 1.0:
            raise ValueError
        r = _log(1.0 - p)

        while 1:
            V = _random()
            if V >= p:
                return 1
            U = _random()
            q = 1.0 - _exp(r * U)
            if V <= q * q:
                # XXX what if V == 0.0 ?
                return intty(1 + _log(V) / _log(q))
            elif V >= q:
                return 1
            else:
                return 2

    return context.compile_internal(builder, logseries_impl, sig, args)


@register
@implement("np.random.negative_binomial", types.int64, types.Kind(types.Float))
def negative_binomial_impl(context, builder, sig, args):
    _gamma = np.random.gamma
    _poisson = np.random.poisson

    def negative_binomial_impl(n, p):
        if n <= 0:
            raise ValueError
        if p < 0.0 or p > 1.0:
            raise ValueError
        Y = _gamma(n, (1.0 - p) / p)
        return _poisson(Y)

    return context.compile_internal(builder, negative_binomial_impl, sig, args)


@register
@implement("np.random.poisson")
@implement("np.random.poisson", types.Kind(types.Float))
def poisson_impl(context, builder, sig, args):
    state_ptr = get_np_state_ptr(context, builder)

    retptr = cgutils.alloca_once(builder, int64_t, name="ret")
    bbcont = cgutils.append_basic_block(builder, "bbcont")
    bbend = cgutils.append_basic_block(builder, "bbend")

    if len(args) == 1:
        lam, = args
        big_lam = builder.fcmp_ordered('>=', lam, ir.Constant(double, 10.0))
        with cgutils.ifthen(builder, big_lam):
            # For lambda >= 10.0, we switch to a more accurate
            # algorithm (see _helperlib.c).
            fnty = ir.FunctionType(int64_t, (rnd_state_ptr_t, double))
            fn = builder.function.module.get_or_insert_function(fnty,
                                                                "numba_poisson_ptrs")
            ret = builder.call(fn, (state_ptr, lam))
            builder.store(ret, retptr)
            builder.branch(bbend)

    builder.branch(bbcont)
    builder.position_at_end(bbcont)

    _random = np.random.random
    _exp = math.exp

    def poisson_impl(lam):
        """Numpy's algorithm for poisson() on small *lam*."""
        if lam < 0.0:
            raise ValueError
        if lam == 0.0:
            return 0
        enlam = _exp(-lam)
        X = 0
        prod = 1.0
        while 1:
            U = _random()
            prod *= U
            if prod <= enlam:
                return X
            X += 1

    if len(args) == 0:
        sig = signature(sig.return_type, types.float64)
        args = (ir.Constant(double, 1.0),)

    ret = context.compile_internal(builder, poisson_impl, sig, args)
    builder.store(ret, retptr)
    builder.branch(bbend)
    builder.position_at_end(bbend)
    return builder.load(retptr)


@register
@implement("np.random.power", types.Kind(types.Float))
def power_impl(context, builder, sig, args):

    def power_impl(a):
        if a <= 0.0:
            raise ValueError
        return math.pow(1 - math.exp(-np.random.standard_exponential()),
                        1./a)

    return context.compile_internal(builder, power_impl, sig, args)


@register
@implement("random.shuffle", types.Kind(types.Array))
def shuffle_impl(context, builder, sig, args):
    _randrange = random.randrange

    def shuffle_impl(arr):
        i = arr.shape[0] - 1
        while i > 0:
            j = _randrange(i + 1)
            arr[i], arr[j] = arr[j], arr[i]
            i -= 1

    return context.compile_internal(builder, shuffle_impl,
                                    sig, args)
