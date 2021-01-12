import argparse
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from typing import Dict, List, Optional, Tuple, Union
from z3 import Solver, Bool, Real, Int, Sum, Implies, Not, And, Or, If
import z3


z3.set_param('parallel.enable', True)
z3.set_param('parallel.threads.max', 8)


def model_to_dict(model: z3.ModelRef) -> Dict[str, Union[float, bool]]:
    ''' Utility function that takes a z3 model and extracts its variables to a
    dict'''
    decls = model.decls()
    res: Dict[str, Union[float, bool]] = {}
    for d in decls:
        val = model[d]
        if type(val) == z3.BoolRef:
            res[d.name()] = bool(val)
        elif type(val) == z3.IntNumRef:
            res[d.name()] = val.as_long()
        else:
            # Assume it is numeric
            decimal = val.as_decimal(100)
            if decimal[-1] == '?':
                decimal = decimal[:-1]
            res[d.name()] = float(decimal)
    return res


class Link:
    def __init__(
        self,
        inps: List[List[Real]],
        rates: List[List[Real]],
        s: Solver,
        C: float,
        D: int,
        buf_min: Optional[float],
        buf_max: Optional[float],
        compose: bool = True,
        name: str = ''
    ):
        ''' Creates a link given `inps` and return (`out`, `lost`). `inps` is
        a list of `inp`, one for each sender. If buf_min is None, there won't
        be any losses. If compose is False, a weaker model that doesn't compose
        is used '''

        assert(len(inps) > 0)
        N = len(inps)
        T = len(inps[0])

        tot_inp = [Real('tot_inp%s_%d' % (name, t)) for t in range(T)]
        outs = [[Real('out%s_%d,%d' % (name, n, t)) for t in range(T)]
                for n in range(N)]
        tot_out = [Real('tot_out%s_%d' % (name, t)) for t in range(T)]
        losts = [[Real('losts%s_%d,%d' % (name, n, t)) for t in range(T)]
                 for n in range(N)]
        tot_lost = [Real('tot_lost%s_%d' % (name, t)) for t in range(T)]
        wasted = [Real('wasted%s_%d' % (name, t)) for t in range(T)]

        if not compose:
            epsilon = Real('epsilon%s' % name)
            s.add(epsilon >= 0)

        max_dt = T
        # If qdel[t][dt] is true, it means that the bytes exiting at t were
        # input at time t - dt. If out[t] == out[t-1], then qdel[t][dt] ==
        # false forall dt. Else, exactly qdel[t][dt] == true for exactly one dt
        # If qdel[t][dt] == true then inp[t - dt - 1] < out[t] <= inp[t - dt]
        qdel = [[Bool('qdel%s_%d,%d' % (name, t, dt)) for dt in range(max_dt)]
                for t in range(T)]

        for t in range(0, T):
            # Things are monotonic
            if t > 0:
                s.add(tot_out[t] >= tot_out[t-1])
                s.add(wasted[t] >= wasted[t-1])
                # Derived from black lines are monotonic
                s.add(wasted[t] <= wasted[t-1] + C)
                s.add(tot_lost[t] >= tot_lost[t-1])
                s.add(tot_inp[t] - tot_lost[t] >= tot_inp[t-1] - tot_lost[t-1])
                for n in range(N):
                    s.add(outs[n][t] >= outs[n][t-1])
                    s.add(losts[n][t] >= losts[n][t-1])

            # Add up the totals
            s.add(tot_inp[t] == Sum(*[x[t] for x in inps]))
            s.add(tot_out[t] == Sum(*[x[t] for x in outs]))
            s.add(tot_lost[t] == Sum(*[x[t] for x in losts]))

            # Constrain out using tot_inp and the black lines
            s.add(tot_out[t] <= tot_inp[t] - tot_lost[t])
            s.add(tot_out[t] <= C * t - wasted[t])
            if t >= D:
                s.add(tot_out[t] >= C * (t - D) - wasted[t - D])
            else:
                # We do not know what wasted was at t < 0, but it couldn't have
                # been bigger than wasted[0], so this bound is valid (and
                # incidentally, tight)
                s.add(tot_out[t] >= C * (t - D) - wasted[0])

            # Condition when wasted is allowed to increase
            if t > 0:
                if compose:
                    s.add(Implies(
                        wasted[t] > wasted[t-1],
                        C * t - wasted[t] >= tot_inp[t] - tot_lost[t]
                    ))
                else:
                    s.add(Implies(
                        wasted[t] > wasted[t-1],
                        tot_inp[t] - tot_lost[t] <= tot_out[t] + epsilon
                    ))

            if buf_min is not None:
                # When can loss happen?
                if t > 0:
                    tot_rate = sum([rates[n][t] for n in range(N)])
                    s.add(Implies(
                        tot_lost[t] > tot_lost[t-1],
                        And(tot_inp[t] - tot_lost[t] >= C*(t-1) - wasted[t-1] + buf_min,
                            tot_rate > C)
                    ))
                else:
                    # Note: Initial loss is unconstrained
                    pass
            else:
                s.add(tot_lost[t] == 0)

            # Enforce buf_max if given
            if buf_max is not None:
                s.add(tot_inp[t] - tot_lost[t] <= C*t - wasted[t] + buf_max)

            # Figure out the time when the bytes being output at time t were
            # first input
            for dt in range(max_dt):
                if t - dt - 1 < 0:
                    s.add(qdel[t][dt] == False)
                    continue

                s.add(qdel[t][dt] == Or(
                    And(
                        tot_out[t] != tot_out[t-1],
                        And(tot_inp[t - dt - 1] - tot_lost[t - dt - 1]
                            < tot_out[t],
                            tot_inp[t - dt] - tot_lost[t - dt] >= tot_out[t])
                    ),
                    And(
                       tot_out[t] == tot_out[t-1],
                       qdel[t-1][dt]
                    )))

            # Figure out how many packets were output from each flow. Ensure
            # that out[n][t] > inp[n][t-dt-1], but leave the rest free for the
            # adversary to choose
            for n in range(N):
                for dt in range(max_dt):
                    if t - dt - 1 < 0:
                        continue
                    s.add(Implies(qdel[t][dt], outs[n][t] > inps[n][t-dt-1]))

        # Initial conditions
        if buf_max is not None:
            s.add(tot_inp[0] <= buf_max)
        if buf_min is not None:
            s.add(tot_inp[0] - tot_lost[0] >= - wasted[0] + buf_min)
        s.add(tot_out[0] == 0)
        for n in range(N):
            s.add(outs[n][0] == 0)

        self.tot_inp = tot_inp
        self.inps = inps
        self.outs = outs
        self.tot_out = tot_out
        self.losts = losts
        self.tot_lost = tot_lost
        self.wasted = wasted
        self.max_dt = max_dt
        self.qdel = qdel


class ModelConfig:
    # Number of flows
    N: int
    # Jitter parameter (in timesteps)
    D: int
    # RTT (in timesteps)
    R: int
    # Number of timesteps
    T: int
    # Link rate
    C: float
    # Packets cannot be dropped below this threshold
    buf_min: Optional[float]
    # Packets have to be dropped above this threshold
    buf_max: Optional[float]
    # Number of dupacks before sender declares loss
    dupacks: Optional[float]
    # Congestion control algorithm
    cca: str
    # If false, we'll use a model is more restrictive but does not compose
    compose: bool
    # Additive increase parameter used by various CCAs
    alpha: Union[float, z3.ArithRef] = 1.0
    # Whether or not to use pacing in various CCA
    pacing: bool
    # If compose is false, wastage can only happen if queue length < epsilon
    epsilon: str

    def __init__(
        self,
        N: int,
        D: int,
        R: int,
        T: int,
        C: float,
        buf_min: Optional[float],
        buf_max: Optional[float],
        dupacks: Optional[float],
        cca: str,
        compose: bool,
        alpha: Optional[float],
        pacing: bool,
        epsilon
    ):
        self.__dict__ = locals()

    @staticmethod
    def get_argparse() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("-N", "--num-flows", type=int, default=1)
        parser.add_argument("-D", type=int, default=1)
        parser.add_argument("-R", "--rtt", type=int, default=1)
        parser.add_argument("-T", "--time", type=int, default=10)
        parser.add_argument("-C", "--rate", type=float, default=1)
        parser.add_argument("--buf-min", type=float, default=None)
        parser.add_argument("--buf-max", type=float, default=None)
        parser.add_argument("--dupacks", type=float, default=None)
        parser.add_argument("--cca", type=str, default="const",
                            choices=["const", "aimd", "copa", "bbr",
                                     "fixed_d"])
        parser.add_argument("--no-compose", action="store_true")
        parser.add_argument("--alpha", type=float, default=None)
        parser.add_argument("--pacing", action="store_const", const=True,
                            default=False)
        parser.add_argument("--epsilon", type=str, default="zero",
                            choices=["zero", "lt_alpha", "lt_half_alpha",
                                     "gt_alpha"])

        return parser

    @classmethod
    def from_argparse(cls, args: argparse.Namespace):
        return cls(
            args.num_flows,
            args.D,
            args.rtt,
            args.time,
            args.rate,
            args.buf_min,
            args.buf_max,
            args.dupacks,
            args.cca,
            not args.no_compose,
            args.alpha,
            args.pacing,
            args.epsilon)


def make_solver(cfg: ModelConfig) -> z3.Solver:
    # Configuration
    N = cfg.N
    C = cfg.C
    D = cfg.D
    R = cfg.R
    T = cfg.T
    buf_min = cfg.buf_min
    buf_max = cfg.buf_max
    dupacks = cfg.dupacks
    cca = cfg.cca
    compose = cfg.compose
    alpha = cfg.alpha
    pacing = cfg.pacing

    inps = [[Real('inp_%d,%d' % (n, t)) for t in range(T)]
            for n in range(N)]
    cwnds = [[Real('cwnd_%d,%d' % (n, t)) for t in range(T)]
             for n in range(N)]
    rates = [[Real('rate_%d,%d' % (n, t)) for t in range(T)]
             for n in range(N)]
    # Number of bytes that have been detected as lost so far (per flow)
    loss_detected = [[Real('loss_detected_%d,%d' % (n, t)) for t in range(T)]
                     for n in range(N)]

    s = Solver()
    lnk = Link(inps, rates, s, C, D, buf_min, buf_max, compose=compose, name='')

    if dupacks is None:
        dupacks = Real('dupacks')
        s.add(dupacks >= 0)
    if alpha is None:
        alpha = Real('alpha')
        s.add(alpha > 0)

    if cfg.epsilon == "zero":
        s.add(Real("epsilon") == 0)
    elif cfg.epsilon == "lt_alpha":
        s.add(Real("epsilon") < alpha)
    elif cfg.epsilon == "lt_half_alpha":
        s.add(Real("epsilon") < alpha * 0.5)
    elif cfg.epsilon == "gt_alpha":
        s.add(Real("epsilon") > alpha)
    else:
        assert(False)

    # Figure out when we can detect losses
    max_loss_dt = T
    for n in range(N):
        for t in range(T):
            for dt in range(max_loss_dt):
                if t > 0:
                    s.add(loss_detected[n][t] >= loss_detected[n][t-1])
                if t - R - dt < 0:
                    continue
                detectable = lnk.inps[n][t-R-dt] - lnk.losts[n][t-R-dt] + dupacks <= lnk.outs[n][t-R]
                s.add(Implies(
                    detectable,
                    loss_detected[n][t] >= lnk.losts[n][t - R - dt]
                ))
                s.add(Implies(
                    Not(detectable),
                    loss_detected[n][t] <= lnk.losts[n][t - R - dt]
                ))
            s.add(loss_detected[n][t] <= lnk.losts[n][t - R])
        for t in range(R):
            s.add(loss_detected[n][t] == 0)

    # Set inps based on cwnds and rates
    for t in range(T):
        for n in range(N):
            # Max value due to cwnd
            if t >= R:
                inp_w = lnk.outs[n][t - R] + loss_detected[n][t] + cwnds[n][t]
            else:
                inp_w = cwnds[n][t]
            if t > 0:
                inp_w = If(inp_w < inps[n][t-1], inps[n][t-1], inp_w)

            # Max value due to rate
            if t > 0:
                inp_r = inps[n][t-1] + rates[n][t]

                # Max of the two values
                s.add(inps[n][t] == If(inp_w < inp_r, inp_w, inp_r))
            else:
                # Unconstrained
                pass

    # Congestion control
    if cca == "const":
        assert(freedom_duration(cfg) == 0)
        for n in range(N):
            for t in range(T):
                s.add(cwnds[n][t] == alpha)
                s.add(rates[n][t] == C * 10)
    elif cca == "aimd":
        # The last send sequence number at which a loss was detected
        last_loss = [[Real('last_loss_%d,%d' % (n, t)) for t in range(T)]
                     for n in range(N)]
        for n in range(N):
            assert(freedom_duration(cfg) == 1)
            s.add(cwnds[n][0] > 0)
            s.add(last_loss[n][0] == 0)
            for t in range(T):
                if pacing:
                    s.add(rates[n][t] == cwnds[n][t] / R)
                else:
                    s.add(rates[n][t] == C * 100)
                if t > 0:
                    # We compare last_loss to outs[t-1-R] (and not outs[t-R])
                    # because otherwise it is possible to react to the same loss
                    # twice
                    if t > R+1:
                        decrease = And(
                            loss_detected[n][t] > loss_detected[n][t-1],
                            last_loss[n][t-1] <= lnk.outs[n][t-1-R]
                        )
                    else:
                        decrease = loss_detected[n][t] > loss_detected[n][t-1]
                    s.add(Implies(decrease,
                                  last_loss[n][t] == lnk.inps[n][t] + dupacks))
                    s.add(Implies(Not(decrease),
                                  last_loss[n][t] == last_loss[n][t-1]))

                    s.add(Implies(decrease, cwnds[n][t] == cwnds[n][t-1] / 2))
                    s.add(Implies(Not(decrease),
                                  cwnds[n][t] == cwnds[n][t-1] + alpha))
    elif cca == "fixed_d":
        for n in range(N):
            for t in range(T):
                diff = 2 * R + 2 * D
                assert(freedom_duration(cfg) == R + diff + 1)
                if t - R - diff < 0:
                    s.add(cwnds[n][t] > 0)
                    s.add(rates[n][t] == cwnds[n][t] / R)
                else:
                    if t % (R + diff) == 0:
                        cwnd = lnk.outs[n][t-R] - lnk.outs[n][t-R-diff] + alpha
                        s.add(cwnds[n][t] == cwnd)
                        s.add(rates[n][t] == cwnds[n][t] / R)
                    else:
                        s.add(cwnds[n][t] == cwnds[n][t-1])
                        s.add(rates[n][t] == rates[n][t-1])
    elif cca == "copa":
        for n in range(N):
            for t in range(T):
                assert(freedom_duration(cfg) == R + D)
                if t - freedom_duration(cfg) < 0:
                    s.add(cwnds[n][t] > 0)
                else:
                    incr_alloweds, decr_alloweds = [], []
                    for dt in range(lnk.max_dt):
                        # Whether we are allowd to increase/decrease
                        incr_allowed = Bool("incr_allowed_%d,%d,%d" % (n, t, dt))
                        decr_allowed = Bool("decr_allowed_%d,%d,%d" % (n, t, dt))
                        # Warning: Adversary here is too powerful if D > 1. Add
                        # a constraint for every point between t-1 and t-1-D
                        assert(D == 1)
                        s.add(incr_allowed
                              == And(
                                  lnk.qdel[t-R][dt],
                                  cwnds[n][t-1] * max(0, dt-1) <= alpha*(R+max(0, dt-1))))
                        s.add(decr_allowed
                              == And(
                                  lnk.qdel[t-R-D][dt],
                                  cwnds[n][t-1] * dt >= alpha * (R + dt)))
                        incr_alloweds.append(incr_allowed)
                        decr_alloweds.append(decr_allowed)
                    # If inp is high at the beginning, qdel can be arbitrarily
                    # large
                    decr_alloweds.append(lnk.tot_out[t-R] < lnk.tot_inp[0])

                    incr_allowed = Or(*incr_alloweds)
                    decr_allowed = Or(*decr_alloweds)

                    # Either increase or decrease cwnd
                    incr = Bool("incr_%d,%d" % (n, t))
                    decr = Bool("decr_%d,%d" % (n, t))
                    s.add(Or(
                        And(incr, Not(decr)),
                        And(Not(incr), decr)))
                    s.add(Implies(incr, incr_allowed))
                    s.add(Implies(decr, decr_allowed))
                    s.add(Implies(incr, cwnds[n][t] == cwnds[n][t-1]+alpha/R))
                    sub = cwnds[n][t-1] - alpha / R
                    s.add(Implies(decr, cwnds[n][t]
                                  == If(sub < alpha, alpha, sub)))

                    # Basic constraints
                    s.add(cwnds[n][t] > 0)
                # Pacing
                s.add(rates[n][t] == cwnds[n][t] / R)
                # s.add(rates[n][t] == 50)

    elif cca == "bbr":
        cycle_start = [[Real(f"cycle_start_{n},{t}") for t in range(T)]
                       for n in range(N)]
        states = [[Int(f"states_{n},{t}") for t in range(T)] for n in range(N)]
        nrtts = [[Int(f"nrtts_{n},{t}") for t in range(T)] for n in range(N)]
        new_rates = [[Real(f"new_rates_{n},{t}") for t in range(T)]
                     for n in range(N)]
        for n in range(N):
            s.add(states[n][0] == 0)
            s.add(nrtts[n][0] == 0)
            assert(freedom_duration(cfg) == R + 1)
            s.add(cycle_start[n][0] == 0)
            for t in range(R + 1):
                s.add(rates[n][t] > 0)
                s.add(cwnds[n][t] == 2 * rates[n][t] * R)

            for t in range(1, T):
                for dt in range(T):
                    if t - R - dt < 0:
                        continue
                    # Has the cycle ended? We look at each dt separately so we
                    # don't need to add non-linear constraints
                    if t - dt == 0:
                        ended = And(cycle_start[n][t-dt] == cycle_start[n][t-1],
                                    lnk.outs[n][t-R] >= cycle_start[n][t-1])
                    else:
                        ended = And(cycle_start[n][t-dt] == cycle_start[n][t-1],
                                    cycle_start[n][t-dt] > cycle_start[n][t-dt-1],
                                    lnk.outs[n][t-R] >= cycle_start[n][t-1])
                    r1 = (lnk.outs[n][t] - lnk.outs[n][t-R-dt]) / (R+dt)
                    r2 = (lnk.outs[n][t] - lnk.outs[n][t-R-dt-1]) / (R+dt+1)

                    # The new rate should be between r1 and r2
                    s.add(Implies(And(ended, r1 >= r2),
                                  And(r1 >= new_rates[n][t],
                                      r2 <= new_rates[n][t])))
                    s.add(Implies(And(ended, r2 >= r1),
                                  And(r1 <= new_rates[n][t],
                                      r2 >= new_rates[n][t])))

                # Find the maximum rate in the last 10 RTTs
                max_rate = [Real(f"max_rate_{n},{t},{dt}") for dt in range(T)]
                s.add(max_rate[0] == new_rates[n][t])
                for dt in range(1, T):
                    if t - dt < 0:
                        continue
                    calc = nrtts[n][t] - nrtts[n][t-dt] < 10
                    s.add(Implies(calc,
                                  max_rate[dt]
                                  == If(max_rate[dt-1] > new_rates[n][t-dt],
                                        max_rate[dt-1], new_rates[n][t-dt])))
                    s.add(Implies(Not(calc),
                                  max_rate[dt] == max_rate[dt-1]))

                if t - R < 0:
                    ended = False
                else:
                    ended = lnk.outs[n][t-R] >= cycle_start[n][t-1]

                # Cycle did not end. Things remain the same
                s.add(Implies(Not(ended),
                              And(new_rates[n][t] == new_rates[n][t-1],
                                  cycle_start[n][t] == cycle_start[n][t-1],
                                  states[n][t] == states[n][t-1],
                                  nrtts[n][t] == nrtts[n][t-1])))

                # Things changed. Update states, cycle_start and cwnds
                s.add(Implies(ended,
                      And(cycle_start[n][t] <= lnk.inps[n][t],
                          cycle_start[n][t] >= lnk.inps[n][t-1],
                          states[n][t] == If(states[n][t-1] < 4,
                                             states[n][t-1] + 1,
                                             0),
                          nrtts[n][t] == nrtts[n][t-1] + 1)))

                # If the cycle *just* ended, then the new rate can be anything
                # between the old rate and the new rate
                in_between_rate = Real(f"in_between_rate_{n},{t}")
                s.add(Implies(And(ended, max_rate[t] >= rates[n][t-1]),
                              And(max_rate[t] >= in_between_rate,
                                  rates[n][t-1] <= in_between_rate)))
                s.add(Implies(And(ended, max_rate[t] <= rates[n][t-1]),
                              And(max_rate[t] <= in_between_rate,
                                  rates[n][t-1] >= in_between_rate)))

                s.add(Implies(Not(ended), in_between_rate == max_rate[t]))

                s.add(rates[n][t] == If(states[n][t] == 0,
                                        1.25 * in_between_rate,
                                        If(states[n][t] == 1,
                                           0.75 * in_between_rate,
                                           in_between_rate)))
                s.add(cwnds[n][t] == 2 * max_rate[t] * R)

    else:
        print("Unrecognized cca")
        exit(1)
    return s


def freedom_duration(cfg: ModelConfig) -> int:
    ''' The amount of time for which the cc can pick any cwnd '''
    if cfg.cca == "const":
        return 0
    elif cfg.cca == "aimd":
        return 1
    elif cfg.cca == "fixed_d":
        return 3 * cfg.R + 2 * cfg.D + 1
    elif cfg.cca == "copa":
        return cfg.R + cfg.D
    elif cfg.cca == "bbr":
        return cfg.R + 1
    else:
        assert(False)


def plot_model(m: Dict[str, Union[float, bool]], cfg: ModelConfig):
    def to_arr(name: str, n: Optional[int] = None) -> np.array:
        if n is None:
            names = [f"{name}_{t}" for t in range(cfg.T)]
        else:
            names = [f"{name}_{n},{t}" for t in range(cfg.T)]
        res = []
        for n in names:
            if n in m:
                res.append(m[n])
            else:
                res.append(-1)
        return np.array(res)

    # Print the constants we picked
    if cfg.dupacks is None:
        print("dupacks = ", m["dupacks"])
    if cfg.cca in ["aimd", "fixed_d", "copa"] and cfg.alpha is None:
        print("alpha = ", m["alpha"])
    if not cfg.compose:
        print("epsilon = ", m["epsilon"])
    for n in range(cfg.N):
        print(f"Init cwnd for flow {n}: ",
              to_arr("cwnd", n)[:freedom_duration(cfg)])

    # Configure the plotting
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True)
    fig.set_size_inches(18.5, 10.5)
    ax1.grid(True)
    ax2.grid(True)
    ax2.set_xticks(range(0, cfg.T))
    ax2.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))

    # Create 3 y-axes in the second plot
    ax2_rtt = ax2.twinx()
    ax2_rate = ax2.twinx()
    ax2.set_ylabel("Cwnd")
    ax2_rtt.set_ylabel("RTT")
    ax2_rate.set_ylabel("Rate")
    ax2_rate.spines["right"].set_position(("axes", 1.05))
    ax2_rate.spines["right"].set_visible(True)

    linestyles = ['--', ':', '-.', '-']
    adj = 0  # np.asarray([C * t for t in range(T)])
    times = [t for t in range(cfg.T)]
    ct = np.asarray([cfg.C * t for t in range(cfg.T)])

    ax1.plot(times, ct - to_arr("wasted"),
             color='black', marker='o', label='Bound', linewidth=3)
    ax1.plot(times[cfg.D:], (ct - to_arr("wasted"))[:-cfg.D],
             color='black', marker='o', linewidth=3)
    ax1.plot(times, to_arr("tot_out"),
             color='red', marker='o', label='Total Egress')
    ax1.plot(times, to_arr("tot_inp"),
             color='blue', marker='o', label='Total Ingress')
    ax1.plot(times, to_arr("tot_inp") - to_arr("tot_lost"),
             color='lightblue', marker='o', label='Total Ingress Accepted')

    # Print incr/decr allowed
    if cfg.cca == "copa":
        print("Copa queueing delay calculation. Format [incr/decr/qdel]")
        for n in range(cfg.N):
            print(f"Flow {n}")
            for t in range(cfg.T):
                print("{:<3}".format(t), end=": ")
                for dt in range(cfg.T):
                    iname = f"incr_allowed_{n},{t},{dt}"
                    dname = f"decr_allowed_{n},{t},{dt}"
                    qname = f"qdel_{t},{dt}"
                    if iname not in m:
                        print(f" - /{int(m[qname])}", end=" ")
                    else:
                        print(f"{int(m[iname])}/{int(m[dname])}/{int(m[qname])}",
                              end=" ")
                print("")

    col_names: List[str] = ["wasted", "tot_out", "tot_inp", "tot_lost"]
    per_flow: List[str] = ["loss_detected", "last_loss", "cwnd", "rate"]
    if cfg.cca == "bbr":
        per_flow.extend(["new_rates", "states"])

    cols: List[Tuple[str, Optional[int]]] = [(x, None) for x in col_names]
    for n in range(cfg.N):
        for x in per_flow:
            if x == "last_loss" and cfg.cca != "aimd":
                continue
            cols.append((x, n))
            col_names.append(f"{x}_{n}")

    print("\n", "=" * 30, "\n")
    print(("t  " + "{:<15}" * len(col_names)).format(*col_names))
    for t, vals in enumerate(zip(*[list(to_arr(*c)) for c in cols])):
        v = ["%.10f" % v for v in vals]
        print(f"{t: <2}", ("{:<15}" * len(v)).format(*v))

    # Calculate RTT (misnomer. Really just qdel)
    rtts_u, rtts_l, rtt_times = [], [], []
    for t in range(cfg.T):
        rtt_u, rtt_l = None, None
        for dt in range(cfg.T):
            if m[f"qdel_{t},{dt}"]:
                assert(rtt_l is None)
                rtt_l = max(0, dt - 1)
            if t >= cfg.D:
                if m[f"qdel_{t-cfg.D},{dt}"]:
                    assert(rtt_u is None)
                    rtt_u = dt
            else:
                rtt_u = 0
        if rtt_u is None:
            rtt_u = rtt_l
        if rtt_l is None:
            rtt_l = rtt_u
        if rtt_l is not None:
            assert(rtt_u is not None)
            rtt_times.append(t)
            rtts_u.append(rtt_u)
            rtts_l.append(rtt_l)
    ax2_rtt.fill_between(rtt_times, rtts_u, rtts_l,
                         color='lightblue', label='RTT', alpha=0.5)
    ax2_rtt.plot(rtt_times, rtts_u, rtts_l,
                 color='blue', marker='o')

    for n in range(cfg.N):
        args = {'marker': 'o', 'linestyle': linestyles[n]}

        ax1.plot(times, to_arr("out", n) - adj,
                 color='red', label='Egress %d' % n, **args)
        ax1.plot(times, to_arr("inp", n) - adj,
                 color='blue', label='Ingress %d' % n, **args)

        ax1.plot(times, to_arr("losts", n) - adj,
                 color='orange', label='Num lost %d' % n, **args)
        ax1.plot(times, to_arr("loss_detected", n)-adj,
                 color='yellow', label='Num lost detected %d' % n, **args)

        ax2.plot(times, to_arr("cwnd", n),
                 color='black', label='Cwnd %d' % n, **args)
        ax2_rate.plot(times, to_arr("rate", n),
                      color='orange', label='Rate %d' % n, **args)

    ax1.legend()
    ax2.legend()
    ax2_rtt.legend()
    ax2_rate.legend()
    plt.savefig('multi_flow_plot.svg')
    plt.show()


if __name__ == "__main__":
    cfg = ModelConfig(
        N=1,
        D=1,
        R=1,
        T=15,
        C=5,
        buf_min=1,
        buf_max=None,
        dupacks=None,
        cca="bbr",
        compose=False,
        alpha=1.0,
        epsilon="zero")
    s = make_solver(cfg)

    # Query constraints

    # Cwnd too small
    # s.add(Real("cwnds_0,%d" % (cfg.T-1)) <= cfg.C * cfg.R - cfg.C)

    # s.add(Real("tot_lost_%d" % (cfg.T-1)) == 0)
    # s.add(Real("tot_lost_0") > 0)

    # Run the model
    satisfiable = s.check()
    print(satisfiable)
    if str(satisfiable) != 'sat':
        exit()
    m = s.model()
    m = model_to_dict(m)

    plot_model(m, cfg)
