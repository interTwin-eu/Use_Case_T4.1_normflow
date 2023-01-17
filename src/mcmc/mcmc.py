# Copyright (c) 2021-2022 Javad Komijani


import torch
import numpy as np
import copy

from normflow import torch_device

from ..lib.combo import seize, estimate_logz, fmt_val_err
from ..lib.stats.resampler import Resampler


# =============================================================================
class MCMCSampler:
    """Perform Markov chain Monte Carlo simulation."""

    def __init__(self, model):
        self._model = model
        self.history = MCMCHistory()
        self._ref = dict(sample=None, logq=None, logp=None, logqp=None)

    @torch.no_grad()
    def sample(self, *args, **kwargs):
        return self.sample_(*args, **kwargs)[0]

    @torch.no_grad()
    def sample_(self, batch_size=1, bookkeeping=False):
        """Return a batch of Monte Carlo Markov Chain samples generated using
        independence Metropolis method.
        Acceptances/rejections occur proportionally to how well/poorly
        the model density matches the desired density.

        The calculations are done by
            1) Drawing raw samples as proposed samples
            2) Apply Metropolis accept/reject to the proposed samples
        """
        y, logq, logp = self._model.raw_dist.sample_(batch_size)

        if bookkeeping:
            self.history.bookkeeping(raw_logq=logq, raw_logp=logp)

        mydict = dict(bookkeeping=bookkeeping)
        y, logq, logp = self._accept_reject_step(y, logq, logp, **mydict)

        if bookkeeping:
            self.history.bookkeeping(logq=logq, logp=logp)

        return y, logq, logp

    @torch.no_grad()
    def _accept_reject_step(self, y, logq, logp, bookkeeping=False):
        # Return (y, logq, logp) after Metropolis accept/reject step to the
        # proposed ones in the input

        ref = self._ref
        logqp_ref = ref['logqp']

        # 2.1) Calculate the accept/reject status of the samples
        accept_seq = Metropolis.calc_accept_status(seize(logq - logp), logqp_ref)

        # 2.2) Handle the first item separately
        if accept_seq[0] == False:
            y[0], logq[0], logp[0] = ref['sample'], ref['logq'], ref['logp']

        # 2.3) Handle the rest items by calculating accept_ind
        accept_ind = Metropolis.calc_accept_indices(accept_seq)

        accept_ind_torch = torch.LongTensor(accept_ind).to(torch_device)
        func = lambda x: x.index_select(0, accept_ind_torch)
        y, logq, logp = func(y), func(logq), func(logp)

        # Update '_ref' dictionary for the next round
        ref['sample'] = y[-1]
        ref['logq'] = logq[-1].item()
        ref['logp'] = logp[-1].item()
        ref['logqp'] = ref['logq'] - ref['logp']

        self.history.bookkeeping(accept_rate=np.mean(accept_seq))  # always save
        if bookkeeping:
            self.history.bookkeeping(accept_seq=accept_seq, accept_ind=accept_ind)

        return y, logq, logp

    @torch.no_grad()
    def serial_sample_generator(self, n_samples, batch_size=16):
        """Generate Monte Carlo Markov Chain samples one by one"""
        unsqz = lambda a, b, c: (a.unsqueeze(0), b.unsqueeze(0), c.unsqueeze(0))
        for i in range(n_samples):
            ind = i % batch_size  # the index of the batch
            if ind == 0:
                y, logq, logp = self.sample_(batch_size)
            yield unsqz(y[ind], logq[ind], logp[ind])

    @torch.no_grad()
    def calc_accept_rate(self, batch_size=1024, n_resamples=10, asstr=False):
        """Calculate acceptance rate from logqp = log(q) - log(p)"""

        # First, draw (raw) samples
        _, logq, logp = self._model.raw_dist.sample_(batch_size)
        logqp = seize(logq - logp)

        # Now calculate the mean and std (by bootstraping) of acceptance rate
        def calc_rate(logqp):
            return np.mean(Metropolis.calc_accept_status(logqp))

        mean, std = Resampler().eval(logqp, fn=calc_rate, n_resamples=n_resamples)

        if asstr:
            return fmt_val_err(mean, std, err_digits=1)
        else:
            return mean, std


# =============================================================================
class BlockedMCMCSampler(MCMCSampler):
    """Perform Markov chain Monte Carlo simulation with blocking."""

    @torch.no_grad()
    def sample(self, *args, **kwargs):
        return self.sample_(*args, **kwargs)[0]

    @torch.no_grad()
    def sample_(self, batch_size=1, n_blocks=1, bookkeeping=False):
        """Return a batch of mcmc samples."""

        prior = self._model.prior
        net_ = self._model.net_
        action = self._model.action

        try:
            x = net_.backward(self._ref['sample'].unsqueeze(0))[0]
            logqp_ref = self._ref['logqp']
        except:
            print("Starting from scratch & setting logqp_ref to None")
            x = prior.sample(1)
            logqp_ref = None

        nvar = prior.nvar
        if isinstance(n_blocks, int):
            block_len = nvar // n_blocks
            assert block_len * n_blocks == nvar
        else:
            block_len = nvar
            n_blocks = 1

        prior.setup_blockupdater(block_len)

        cfgs = torch.empty((batch_size, *prior.shape))
        logq = torch.empty((batch_size,))
        logp = torch.empty((batch_size,))
        accept_seq = np.empty((batch_size, n_blocks), dtype=bool)

        for ind in range(batch_size):
            accept_seq[ind], logqp_ref = self.sweep(x, n_blocks, logqp_ref)  # in-place sweeper
            y, logJ = net_(x)
            logq[ind] = prior.log_prob(x) - logJ
            logp[ind] = -action(y)
            cfgs[ind] = y

        # update '_ref' dictionary for the next round
        self._ref['sample'] = y[-1]
        self._ref['logq'] = logq[-1].item()
        self._ref['logp'] = logp[-1].item()
        self._ref['logqp'] = (logq[-1] - logp[-1]).item()

        self.history.bookkeeping(accept_rate=np.mean(accept_seq))  # always save
        if bookkeeping:
            self.history.bookkeeping(logq=logq, logp=logp)
            self.history.bookkeeping(accept_seq=accept_seq.ravel())

        return cfgs, logq, logp

    @torch.no_grad()
    def sweep(self, x, n_blocks=1, logqp_ref=None):
        """In-place sweeper."""
        prior = self._model.prior
        net_ = self._model.net_
        action = self._model.action

        accept_seq = np.empty(n_blocks, dtype=bool)
        lrand_arr = np.log(np.random.rand(n_blocks))

        for ind in range(n_blocks):
            prior.blockupdater(x, ind)  # in-place updater
            y, logJ = net_(x)
            logq = prior.log_prob(x) - logJ
            logp = -action(y)
            # Metropolis acceptance condition:
            if ind == 0 and logqp_ref is None:
                accept_seq[ind] = True
            else:
                accept_seq[ind] = lrand_arr[ind] < logqp_ref - (logq - logp)[0]
            if accept_seq[ind]:
                logqp_ref = (logq - logp).item()
            else:
                prior.blockupdater.restore(x, ind)

        return accept_seq, logqp_ref


# =============================================================================
class MCMCHistory:
    """For bookkeeping of Perform Markov chain Monte Carlo simulation."""

    def __init__(self):
        self.reset_history()

    def reset_history(self):
        self.logq = []
        self.logp = []
        self.raw_logq = []
        self.raw_logp = []
        self.accept_seq = []
        self.accept_ind = []
        self.accept_rate = []

    def report_summary(self, since=0, asstr=False):

        if asstr:
            fmt = lambda mean, std: fmt_val_err(mean, std, err_digits=2)
        else:
            fmt = lambda mean, std: (mean, std)

        logqp = torch.tensor(self.logq[-1] - self.logp[-1])  # estimate_logz
        accept_rate = torch.tensor(self.accept_rate)
        mean_std = lambda t: (t.mean().item(), t.std().item())

        report = {'logqp': fmt(*mean_std(logqp)),
                  'logz': fmt(*estimate_logz(logqp)),
                  'accept_rate': fmt(*mean_std(accept_rate))
                  }
        return report

    def bookkeeping(self,
            logq=None,
            logp=None,
            raw_logq=None,
            raw_logp=None,
            accept_seq=None,
            accept_rate=None,
            accept_ind=None
            ):

        if raw_logq is not None:
            # make a copy of the raw one in case it is manually changed
            self.raw_logq.append(copy.copy(seize(raw_logq)))

        if raw_logp is not None:
            # make a copy of the raw one in case it is manually changed
            self.raw_logp.append(copy.copy(seize(raw_logp)))

        if logq is not None:
            self.logq.append(seize(logq))

        if logp is not None:
            self.logp.append(seize(logp))

        if accept_rate is not None:
            self.accept_rate.append(accept_rate)

        if accept_seq is not None:
            self.accept_seq.append(accept_seq)

        if accept_ind is not None:
            self.accept_ind.append(accept_ind)

    @property
    def logqp(self):
        return [(logq - logp) for (logq, logp) in zip(self.logq, self.logp)]

    @property
    def raw_logqp(self):
        return [(logq - logp) for (logq, logp) in zip(self.raw_logq, self.raw_logp)]


# =============================================================================
class Metropolis:
    """
    To perform Metropolis-Hastings accept/reject step in Markov chain Monte
    Carlo simulation.
    """

    @staticmethod
    @torch.no_grad()
    def calc_accept_status(logqp, logqp_ref=None):
        """Returns accept/reject using Metropolis algorithm."""
        # Much faster if inputs are np.ndarray & python number (NO tensor)
        if logqp_ref is None:
            logqp_ref = logqp[0]
        status = np.empty(len(logqp), dtype=bool)
        rand_arr = np.log(np.random.rand(logqp.shape[0]))
        for i, logqp_i in enumerate(logqp):
            status[i] = rand_arr[i] < (logqp_ref - logqp_i)
            if status[i]:
                logqp_ref = logqp_i
        return status

    @staticmethod
    def calc_accept_indices(accept_seq):
        """Return indices of output of Metropolis-Hasting accept/reject step."""
        indices = np.zeros(len(accept_seq), dtype=int)
        cntr = 0
        for ind, accept in enumerate(accept_seq):
            if accept:
                cntr = ind
            indices[ind] = cntr
        return indices

    @staticmethod
    def calc_accept_count(status):
        """Count how many repetition till next accepted configuration."""
        ind = np.where(status)[0]  # index of True ones
        mul = ind[1:] - ind[:-1]  # count except for the last
        return ind[0], list(mul) + [len(states) - ind[-1]]


# =============================================================================