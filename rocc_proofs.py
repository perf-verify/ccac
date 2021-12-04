from z3 import And

from cache import run_query
from model import make_solver
from config import ModelConfig


def prove_steady_state(timeout=10):
    c = ModelConfig.default()
    c.cca = "rocc"
    c.compose = True
    c.calculate_qdel = True
    c.unsat_core = False
    c.N = 1

    # The bigger this value is, the longer our T needs to be for some proofs
    # and the bigger max_queue
    max_min_rtt = 11
    max_queue = c.C * (max_min_rtt + 4)

    # Queue length decreases if it is too high
    x = max_min_rtt + 2*c.R + c.D + 1 + 1
    c.T = x + 1
    assert(x < c.T)
    s, v = make_solver(c)
    s.add(v.alpha < 1)
    s.add(v.cv.minrtt_f[0][0] <= max_min_rtt)
    # s.add(v.cv.probe[0] >= x)
    # s.add(v.cv.probe[0] + max_min_rtt + c.D < c.T)
    # assert(x + max_min_rtt + c.D < c.T)
    s.add(And(v.A_f[0][x] - v.L_f[0][x] - v.S_f[0][x] > max_queue,
              v.A_f[0][-1] - v.L_f[0][-1] - v.S_f[0][-1] >
              v.A_f[0][x] - v.L_f[0][x] - v.S_f[0][x] + c.C))
    print("Proving that queue length decreases")
    qres = run_query(s, c, timeout)
    print(qres.satisfiable)
    assert(qres.satisfiable == "unsat")

    # Number of undetected losses decreases
    c.T = 10
    s, v = make_solver(c)
    s.add(v.A_f[0][0] - v.L_f[0][0] - v.S_f[0][0] <= max_queue)
    s.add(And(v.L_f[0][0] - v.Ld_f[0][0] > 0,
              v.L_f[0][-1] - v.Ld_f[0][-1] > 0,
              v.L_f[0][-1] - v.Ld_f[0][-1] >
              v.L_f[0][0] - v.Ld_f[0][0] + c.C))
    print("Proving that number of undetected losses decreases")
    qres = run_query(s, c, timeout)
    print(qres.satisfiable)
    assert(qres.satisfiable == "unsat")

    # If the min rtt estimate was too high, it would have decreased by the end
    # if a min rtt probe happened in the time duration
    c.T = 10
    s, v = make_solver(c)
    s.add(v.alpha < 1)
    s.add(v.cv.probe[0] >= 2)
    s.add(v.L_f[0][0] - v.Ld_f[0][0] == 0)
    s.add(And(v.cv.minrtt_f[0][0] > c.D, v.cv.minrtt_f[0][0] < max_min_rtt,
              v.cv.minrtt_f[0][-1] >= v.cv.minrtt_f[0][0]))
    s.add(v.A_f[0][0] - v.L_f[0][0] - v.S_f[0][0] <= max_queue)
    print("Proving that min rtt will decrease if it is too high")
    qres = run_query(s, c, timeout)
    print(qres.satisfiable)
    from plot import plot_model
    plot_model(qres.model, c)
    assert(qres.satisfiable == "unsat")

    c.T = 10
    s, v = make_solver(c)
    s.add(v.alpha < 1)
    s.add(v.cv.probe[0] == -1)


if __name__ == "__main__":
    prove_steady_state()
