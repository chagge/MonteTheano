"""
"""
import __builtin__
import copy

import numpy

import theano
from theano import tensor

from for_theano import elemwise_cond
from for_theano import ancestors
from pdfreg import log_pdf, is_random_var, full_log_likelihood

class Updates(dict):
    def __add__(self, other):
        rval = Updates(self)
        rval += other  # see: __iadd__
        return rval
    def __iadd__(self, other):
        d = dict(other)
        for k,v in d.items():
            if k in self:
                raise KeyError()

            self[k] = v

def rv_dist_name(rv):
    try:
        return rv.owner.op.dist_name
    except AttributeError:
        try:
            return rv.owner.op.fn.__name__
        except AttributeError:
            raise TypeError('rv not recognized as output of RandomFunction')

class RandomStreams(object):

    samplers = {}
    pdfs = {}
    ml_handlers = {}
    params_handlers = {}
    local_proposals = {}

    def __init__(self, seed):
        self.state_updates = []
        self.default_instance_seed = seed
        self.seed_generator = numpy.random.RandomState(seed)
        self.default_updates = {}

    def shared(self, val, **kwargs):
        rval = theano.shared(val, **kwargs)
        return rval

    def sharedX(self, val, **kwargs):
        rval = theano.shared(
                numpy.asarray(val, dtype=theano.config.floatX),
                **kwargs)
        return rval

    def new_shared_rstate(self):
        seed = int(self.seed_generator.randint(2**30))
        rval = theano.shared(numpy.random.RandomState(seed))
        return rval

    def add_default_update(self, used, recip, new_expr):
        if used not in self.default_updates:
            self.default_updates[used] = {}
        self.default_updates[used][recip] = new_expr
        used.update = (recip, new_expr) # necessary?
        recip.default_update = new_expr
        self.state_updates.append((recip, new_expr))

    def sample(self, dist_name, *args, **kwargs):
        handler = self.samplers[dist_name]
        out = handler(self, *args, **kwargs)
        return out

    def seed(self, seed=None):
        """Re-initialize each random stream

        :param seed: each random stream will be assigned a unique state that depends
        deterministically on this value.

        :type seed: None or integer in range 0 to 2**30

        :rtype: None
        """
        if seed is None:
            seed = self.default_instance_seed

        seedgen = numpy.random.RandomState(seed)
        for old_r, new_r in self.state_updates:
            old_r_seed = seedgen.randint(2**30)
            old_r.set_value(numpy.random.RandomState(int(old_r_seed)),
                    borrow=True)

    def pdf(self, rv, sample, **kwargs):
        """
        Return the probability (density) that random variable `rv`, returned by
        a call to one of the sampling routines of this class takes value `sample`
        """
        if rv.owner:
            dist_name = rv_dist_name(rv)
            pdf = self.pdfs[dist_name]
            return pdf(rv.owner, sample, kwargs)
        else:
            raise TypeError('rv not recognized as output of RandomFunction')

    def ml(self, rv, sample, weights=None):
        """
        Return an Updates object mapping distribution parameters to expressions
        of their maximum likelihood values.
        """
        if rv.owner:
            dist_name = rv_dist_name(rv)
            pdf = self.ml_handlers[dist_name]
            return pdf(rv.owner, sample, weights=weights)
        else:
            raise TypeError('rv not recognized as output of RandomFunction')

    def params(self, rv):
        """
        Return an Updates object mapping distribution parameters to expressions
        of their maximum likelihood values.
        """
        if rv.owner:
            return self.params_handlers[rv_dist_name(rv)](rv.owner)
        else:
            raise TypeError('rv not recognized as output of RandomFunction')

    def local_proposal(rv, sample, **kwargs):
        """
        Return the probability (density) that random variable `rv`, returned by
        a call to one of the sampling routines of this class takes value `sample`
        """
        raise NotImplementedError()

    #
    # N.B. OTHER METHODS (samplers) ARE INSTALLED HERE BY
    # - register_sampler
    # - rng_register
    #

def pdf(rv, sample):
    return rv.rstreams.pdf(rv, sample)


def ml_updates(rv, sample, weights=None):
    return rv.rstreams.ml_updates(rv, sample, weights=weights)


def params(rv):
    return rv.rstreams.params(rv)

def register_sampler(dist_name, f):
    """
    Inject a sampling function into RandomStreams for the distribution with name
    f.__name__
    """
    # install an instancemethod on the RandomStreams class
    # that is a shortcut for something like
    # self.sample('uniform', *args, **kwargs)

    def sampler(self, *args, **kwargs):
        return self.sample(dist_name, *args, **kwargs)
    setattr(RandomStreams, dist_name, sampler)

    if dist_name in RandomStreams.samplers:
        # TODO: allow for multiple handlers?
        raise KeyError(dist_name)
    RandomStreams.samplers[dist_name] = f
    return f


def register_pdf(dist_name, f):
    if dist_name in RandomStreams.pdfs:
        # TODO: allow for multiple handlers?
        raise KeyError(dist_name, RandomStreams.pdfs[dist_name])
    RandomStreams.pdfs[dist_name] = f
    return f


def register_ml(dist_name, f):
    if dist_name in RandomStreams.ml_handlers:
        # TODO: allow for multiple handlers?
        raise KeyError(dist_name, RandomStreams.ml_handlers[dist_name])
    RandomStreams.ml_handlers[dist_name] = f
    return f


def register_params(dist_name, f):
    if dist_name in RandomStreams.params_handlers:
        # TODO: allow for multiple handlers?
        raise KeyError(dist_name, RandomStreams.params_handlers[dist_name])
    RandomStreams.params_handlers[dist_name] = f
    return f


def rng_register(f):
    if f.__name__.endswith('_sampler'):
        dist_name = f.__name__[:-len('_sampler')]
        return register_sampler(dist_name, f)

    elif f.__name__.endswith('_pdf'):
        dist_name = f.__name__[:-len('_pdf')]
        return register_pdf(dist_name, f)

    elif f.__name__.endswith('_ml'):
        dist_name = f.__name__[:-len('_ml')]
        return register_ml(dist_name, f)

    elif f.__name__.endswith('_params'):
        dist_name = f.__name__[:-len('_params')]
        return register_params(dist_name, f)

    else:
        raise ValueError("function name suffix not recognized", f.__name__)


class ClobberContext(object):
    def __enter__(self):
        not hasattr(self, 'old')
        self.old = {}
        for name in self.clobber:
            if hasattr(__builtin__, name):
                self.old[name] = getattr(__builtin__, name)
            if hasattr(self.obj, name):
                setattr(__builtin__, name, getattr(self.obj, name))
        return self.obj

    def __exit__(self, e_type, e_val, e_traceback):
        for name in self.clobber:
            if name in self.old:
                setattr(__builtin__, name, self.old[name])
            elif hasattr(__builtin__, name):
                delattr(__builtin__, name)
        del self.old


class srng_globals(ClobberContext):
    def __init__(self, obj):
        if isinstance(obj, int):
            self.obj = RandomStreams(23424)
        else:
            self.obj = obj
        self.clobber = self.obj.samplers.keys() + ['pdf']


#####################
# Stock distributions
#####################


# -------
# Uniform
# -------


@rng_register
def uniform_sampler(rstream, low=0.0, high=1.0, shape=None, ndim=None, dtype=None):
    rstate = rstream.new_shared_rstate()
    new_rstate, out = tensor.raw_random.uniform(rstate, shape, low, high, ndim, dtype)
    rstream.add_default_update(out, rstate, new_rstate)
    return out


@rng_register
def uniform_pdf(node, sample, kw):
    # make sure that the division is done at least with float32 precision
    one = tensor.as_tensor_variable(numpy.asarray(1, dtype='float32'))
    rstate, shape, low, high = node.inputs
    rval = elemwise_cond(
        0,                sample < low,
        one / (high-low), sample <= high,
        0)
    return rval

@rng_register
def uniform_ml(node, sample, weights):
    rstate, shape, low, high = node.inputs
    return Updates({
        low: sample.min(),
        high: sample.max()})

@rng_register
def uniform_params(node):
    rstate, shape, low, high = node.inputs
    return [low, high]

# ------
# Normal
# ------


@rng_register
def normal_sampler(rstream, mu=0.0, sigma=1.0, shape=None, ndim=0, dtype=None):
    if not isinstance(mu, theano.Variable):
        mu = tensor.shared(numpy.asarray(mu, dtype=theano.config.floatX))
    if not isinstance(mu, theano.Variable):
        sigma = tensor.shared(numpy.asarray(sigma, dtype=theano.config.floatX))
    rstate = rstream.new_shared_rstate()
    new_rstate, out = tensor.raw_random.normal(rstate, shape, mu, sigma, dtype=dtype)
    rstream.add_default_update(out, rstate, new_rstate)
    return out


@rng_register
def normal_pdf(node, sample, kw):
    # make sure that the division is done at least with float32 precision
    one = tensor.as_tensor_variable(numpy.asarray(1, dtype='float32'))
    rstate, shape, mu, sigma = node.inputs
    Z = tensor.sqrt(2 * numpy.pi * sigma**2)
    E = 0.5 * ((mu - sample)/(one*sigma))**2
    return tensor.exp(-E) / Z

@rng_register
def normal_ml(node, sample, weights):
    rstate, shape, mu, sigma = node.inputs
    eps = 1e-8
    if weights is None:
        new_mu = tensor.mean(sample)
        new_sigma = tensor.std(sample)

    else:
        denom = tensor.maximum(tensor.sum(weights), eps)
        new_mu = tensor.sum(sample*weights) / denom
        new_sigma = tensor.sqrt(
                tensor.sum(weights * (sample - new_mu)**2)
                / denom)
    return Updates({
        mu: new_mu,
        sigma: new_sigma})

def full_sample(s_rng, outputs ):
    all_vars = ancestors(outputs)
    assert outputs[0] in all_vars
    RVs = [v for v in all_vars if is_random_var(v)]
    rdict = dict([(v, v) for v in RVs])

    if True:
        # outputs is same
        raise NotImplementedError()
    elif isinstance(size, int):
        # use scan
        raise NotImplementedError()
    else:
        n_steps = theano.tensor.prod(size)
        # use scan for n_steps
        #scan_outputs = ...
        outputs = scan_outputs[:len(outputs)]
        s_RVs = scan_outputs[len(outputs):]
        # reshape so that leading dimension goes from n_steps -> size
        raise NotImplementedError()
    return outputs, rdict

# Sample the generative model and return "outputs" for cases where "condition" is met.
# If no condition is given, it just samples from the model
# The outputs can be a single TheanoVariable or a list of TheanoVariables.
# The function returns a single sample or a list of samples, depending on "outputs"; and an updates dictionary.
def rejection_sample(outputs, condition = None):
    if isinstance(outputs, tensor.TensorVariable):
        init = [0]
    else:
        init = [0]*len(outputs)
    if condition is None:
        # TODO: I am just calling scan to get updates, can't I create this myself?
        # output desired RVs when condition is met
        def rejection():
            return outputs

        samples, updates = theano.scan(rejection, outputs_info = init, n_steps = 1)
    else:
        # output desired RVs when condition is met
        def rejection():
            return outputs, {}, theano.scan_module.until(condition)
        samples, updates = theano.scan(rejection, outputs_info = init, n_steps = 1000)
    if isinstance(samples, tensor.TensorVariable):
        sample = samples[-1]
    else:
        sample = [s[-1] for s in samples]
    return sample, updates


@rng_register
def normal_params(node):
    rstate, shape, mu, sigma = node.inputs
    return [mu, sigma]

# ---------
# LogNormal
# ---------


@rng_register
def lognormal_sampler(s_rstate, mu=0.0, sigma=1.0, shape=None, ndim=None, dtype=None):
    """
    Sample from a normal distribution centered on avg with
    the specified standard deviation (std).

    If the size argument is ambiguous on the number of dimensions, ndim
    may be a plain integer to supplement the missing information.

    If size is None, the output shape will be determined by the shapes
    of avg and std.

    If dtype is not specified, it will be inferred from the dtype of
    avg and std, but will be at least as precise as floatX.
    """
    mu = tensor.as_tensor_variable(mu)
    sigma = tensor.as_tensor_variable(sigma)
    if dtype == None:
        dtype = tensor.scal.upcast(
                theano.config.floatX, mu.dtype, sigma.dtype)
    ndim, shape, bcast = tensor.raw_random._infer_ndim_bcast(
            ndim, shape, mu, sigma)
    op = tensor.raw_random.RandomFunction('lognormal',
            tensor.TensorType(dtype=dtype, broadcastable=bcast))
    return op(s_rstate, shape, mu, sigma)


@rng_register
def lognormal_pdf(node, x, kw):
    r, shape, mu, sigma = node.inputs
    Z = sigma * x * numpy.sqrt(2 * numpy.pi)
    E = 0.5 * ((tensor.log(x) - mu) / sigma)**2
    return tensor.exp(-E)/Z


# -----------
# Categorical
# -----------


class Categorical(theano.Op):
    dist_name = 'categorical'
    def __init__(self, destructive, otype):
        self.destructive = destructive
        self.otype = otype
        if destructive:
            self.destroy_map = {0:[0]}
        else:
            self.destroy_map = {}

    def __eq__(self, other):
        return (type(self) == type(other)
                and self.destructive == other.destructive
                and self.otype == other.otype)

    def __hash__(self):
        return hash((type(self), self.destructive, self.otype))

    def make_node(self, s_rstate, p):
        p = tensor.as_tensor_variable(p)
        if p.ndim != 1: raise NotImplementedError()
        return theano.gof.Apply(self,
                [s_rstate, p],
                [s_rstate.type(), self.otype()])

    def perform(self, node, inputs, outstor):
        rng, p = inputs
        if not self.destructive:
            rng = copy.deepcopy(rng)
        counts = rng.multinomial(pvals=p, n=1)
        oval = numpy.where(counts)[0][0]
        outstor[0][0] = rng
        outstor[1][0] = self.otype.filter(oval, allow_downcast=True)


@rng_register
def categorical_sampler(rstate, p, shape=None, ndim=None, dtype='int32'):
    if shape != None:
        raise NotImplementedError()
    op = Categorical(False,
            tensor.TensorType(
                broadcastable=(),
                dtype=dtype))
    return op(rstate, p)


@rng_register
def categorical_pdf(node, sample, kw):
    """
    Return a random integer from 0 .. N-1 inclusive according to the
    probabilities p[0] .. P[N-1].

    This is formally equivalent to numpy.where(multinomial(n=1, p))
    """
    # WARNING: I think the p[-1] is not used, but assumed to be p[:-1].sum()
    s_rstate, p = node.inputs
    return p[sample]


# ---------
# Dirichlet
# ---------


class Dirichlet(theano.Op):
    dist_name = 'dirichlet'
    def __init__(self, destructive):
        self.destructive = destructive
        if destructive:
            self.destroy_map = {0:[0]}
        else:
            self.destroy_map = {}

    def __eq__(self, other):
        return (type(self) == type(other)
                and self.destructive == other.destructive)

    def __hash__(self):
        return hash((type(self), self.destructive))

    def make_node(self, s_rstate, alpha):
        alpha = tensor.as_tensor_variable(alpha)
        if alpha.ndim != 1: raise NotImplementedError()
        return theano.gof.Apply(self,
                [s_rstate, alpha],
                [s_rstate.type(), alpha.type()])

    def perform(self, node, inputs, outstor):
        rng, alpha = inputs
        if not self.destructive:
            rng = copy.deepcopy(rng)
        oval = rng.dirichlet(alpha=alpha).astype(alpha.dtype)
        outstor[0][0] = rng
        outstor[1][0] = oval


@rng_register
def dirichlet_sampler(rstate, alpha, shape=None, ndim=None, dtype=None):
    if shape != None:
        raise NotImplementedError()
    if dtype != None:
        raise NotImplementedError()
    op = Dirichlet(False)
    return op(rstate, alpha)


@rng_register
def dirichlet_pdf(node, sample, kw):
    """

    http://en.wikipedia.org/wiki/Dirichlet_distribution

    """
    raise NotImplementedError()

def mh_sample(s_rng, outputs, observations = {}):
    # TODO: should there be a size variable here?
    # TODO: implement lag and burn-in
    # TODO: implement size
    """
    Return a dictionary mapping random variables to their sample values.
    """

    all_vars = ancestors(list(outputs) + list(observations.keys()))
    for o in observations:
        assert o in all_vars
        if not is_random_var(o):
            raise TypeError(o)

    free_RVs = [v for v in RVs if v not in observations]

    # TODO: sample from the prior to initialize these guys?
    # free_RVs_state = [theano.shared(v) for v in free_RVs]
    # TODO: how do we infer shape?
    free_RVs_state = [theano.shared(0.5*numpy.ones(shape=())) for v in free_RVs]
    free_RVs_prop = [s_rng.normal(size = (), std = .1) for v in free_RVs]

    log_likelihood = theano.shared(numpy.array(float('-inf')))

    U = s_rng.uniform(size=(), low=0, high=1.0)

    # TODO: can we pre-generate the noise
    def mcmc(ll, *frvs):
        # TODO: implement generic proposal distributions
        # TODO: how do we infer shape?
        proposals = [(rvs + rvp) for rvs,rvp in zip(free_RVs_state, free_RVs_prop)]

        full_observations = dict(observations)
        full_observations.update(dict([(rv, s) for rv, s in zip(free_RVs, proposals)]))

        new_log_likelihood = full_log_likelihood(observations = full_observations)

        accept = tensor.or_(new_log_likelihood > ll, U <= tensor.exp(new_log_likelihood - ll))

        return [tensor.switch(accept, new_log_likelihood, ll)] + \
               [tensor.switch(accept, p, f) for p, f in zip(proposals, frvs)], \
               {}, theano.scan_module.until(accept)

    samples, updates = theano.scan(mcmc, outputs_info = [log_likelihood] + free_RVs_state, n_steps = 10000000)
    updates[log_likelihood] = samples[0][-1]
    updates.update(dict([(f, s[-1]) for f, s in zip(free_RVs_state, samples[1:])]))
    
    return [free_RVs_state[free_RVs.index(out)] for out in outputs], log_likelihood, updates

def hybridmc_sample(s_rng, outputs, observations = {}):
    # TODO: should there be a size variable here?
    # TODO: implement lag and burn-in
    # TODO: implement size
    """
    Return a dictionary mapping random variables to their sample values.
    """

    all_vars = ancestors(list(outputs) + list(observations.keys()))
    
    for o in observations:
        assert o in all_vars
        if not is_random_var(o):
            raise TypeError(o)

    RVs = [v for v in all_vars if is_random_var(v)]

    free_RVs = [v for v in RVs if v not in observations]
    
    free_RVs_state = [theano.shared(0.5*numpy.ones(shape=())) for v in free_RVs]    
    free_RVs_prop = [s_rng.normal(size = (), std = 1) for v in free_RVs]    
    
    log_likelihood = theano.shared(numpy.array(float('-inf')))
    
    U = s_rng.uniform(size=(), low=0, high=1.0)
    
    epsilon = numpy.sqrt(2*0.03)
    def mcmc(ll, *frvs):
        full_observations = dict(observations)
        full_observations.update(dict([(rv, s) for rv, s in zip(free_RVs, frvs)]))
        loglik = -full_log_likelihood(observations = full_observations)

        proposals = free_RVs_prop
        H = tensor.add(*[tensor.sum(tensor.sqr(p)) for p in proposals])/2. + loglik

# -- this should be an inner loop
        g = tensor.grad(loglik, frvs)
        proposals = [(p - epsilon*g/2.) for p, g in zip(proposals, g)]

        rvsp = [(rvs + epsilon*rvp) for rvs,rvp in zip(frvs, proposals)]
        
        full_observations = dict(observations)
        full_observations.update(dict([(rv, s) for rv, s in zip(free_RVs, rvsp)]))
        new_loglik = -full_log_likelihood(observations = full_observations)
        
        gnew = tensor.grad(new_loglik, rvsp)
        proposals = [(p - epsilon*gn/2.) for p, gn in zip(proposals, gnew)]
# --
        
        Hnew = tensor.add(*[tensor.sum(tensor.sqr(p)) for p in proposals])/2. + new_loglik

        dH = Hnew - H
        accept = tensor.or_(dH < 0., U < tensor.exp(-dH))

        return [tensor.switch(accept, -new_loglik, ll)] + \
            [tensor.switch(accept, p, f) for p, f in zip(rvsp, frvs)], \
            {}, theano.scan_module.until(accept)

    samples, updates = theano.scan(mcmc, outputs_info = [log_likelihood] + free_RVs_state, n_steps = 10000000)
    
    updates[log_likelihood] = samples[0][-1]
    updates.update(dict([(f, s[-1]) for f, s in zip(free_RVs_state, samples[1:])]))
    
    return [free_RVs_state[free_RVs.index(out)] for out in outputs], log_likelihood, updates
    
    
