"""Quorum election — unit tests of the pure rules plus a partition simulator that drives the
real `election_manager.Node` class through crashes, splits and heals on a fake clock.

The safety invariant checked after EVERY simulated step: at most one primary per term.
"""

import copy
import random

import pytest

from app import election as E
from app.election_manager import Node


# ───────────────────────────── pure rules ─────────────────────────────

def cfg_of(n_voters, n_nonvoting=0, **kw):
    ms = [E.Member(node_id=f"n{i+1}", address=f"n{i+1}", priority=i + 1, votes=1) for i in range(n_voters)]
    ms += [E.Member(node_id=f"x{i+1}", address=f"x{i+1}", priority=0, votes=0) for i in range(n_nonvoting)]
    return E.Config(members=ms, **kw)


@pytest.mark.parametrize("voters,mode,q", [
    (1, E.STANDALONE, 1), (2, E.MANUAL, 2), (3, E.AUTOMATIC, 2), (4, E.MANUAL, 3),
    (5, E.AUTOMATIC, 3), (6, E.MANUAL, 4), (7, E.AUTOMATIC, 4),
])
def test_mode_and_quorum(voters, mode, q):
    c = cfg_of(voters)
    assert E.failover_mode(c)[0] == mode
    assert E.quorum(len(c.voters)) == q
    assert E.validate_config(c) is None


def test_non_voting_members_do_not_change_mode():
    assert E.failover_mode(cfg_of(3, 1))[0] == E.AUTOMATIC
    assert E.failover_mode(cfg_of(2, 3))[0] == E.MANUAL


def test_config_validation():
    assert "more than 7" in E.validate_config(cfg_of(8))
    c = cfg_of(3)
    c.members[0].votes = 0
    assert "non-voting" in E.validate_config(c)
    c = cfg_of(3)
    c.election_timeout_s = 4
    assert "3 heartbeats" in E.validate_config(c)


def test_membership_changes_bump_version_and_validate():
    c = cfg_of(3)
    c2 = E.add_member(c, E.Member("n4", "n4", priority=4, votes=1))
    assert c2.version == c.version + 1 and E.failover_mode(c2)[0] == E.MANUAL
    c3 = E.update_member(c2, "n4", votes=0)
    assert c3.member("n4").priority == 0 and E.failover_mode(c3)[0] == E.AUTOMATIC
    with pytest.raises(ValueError):
        E.add_member(c3, E.Member("n4", "n4"))
    with pytest.raises(ValueError):
        E.remove_member(c3, "nope")
    assert E.next_priority(c3) == 4


def test_one_vote_per_term():
    st = E.State("n2", cfg_of(3), term=5)
    r1 = E.grant_vote(st, {"term": 6, "candidate_id": "n1"}, now=100)
    r2 = E.grant_vote(st, {"term": 6, "candidate_id": "n3"}, now=100)
    assert r1["granted"] and not r2["granted"] and "already voted" in r2["reason"]
    assert E.grant_vote(st, {"term": 6, "candidate_id": "n1"}, now=100)["granted"]  # idempotent re-ask


def test_stale_term_and_live_primary_refused():
    st = E.State("n2", cfg_of(3), term=5)
    assert not E.grant_vote(st, {"term": 4, "candidate_id": "n1"}, 0)["granted"]
    st.leader_id, st.last_leader_contact = "n1", 100.0
    r = E.answer_pre_vote(st, {"term": 6, "candidate_id": "n3"}, now=105.0)
    assert not r["granted"] and "alive" in r["reason"]
    r = E.answer_pre_vote(st, {"term": 6, "candidate_id": "n3", "transfer": True}, now=105.0)
    assert r["granted"]


def test_pre_vote_never_changes_state():
    st = E.State("n2", cfg_of(3), term=5)
    E.answer_pre_vote(st, {"term": 6, "candidate_id": "n1"}, 0)
    assert st.term == 5 and st.voted_for is None


def test_no_elections_outside_automatic_mode():
    for n in (2, 4):
        st = E.State("n2", cfg_of(n), term=1)
        assert not E.answer_pre_vote(st, {"term": 2, "candidate_id": "n1"}, 0)["granted"]


def test_non_voter_and_priority_zero_rules():
    c = cfg_of(3, 1)
    st = E.State("x1", c, term=1)
    assert not E.grant_vote(st, {"term": 2, "candidate_id": "n1"}, 0)["granted"]     # x1 cannot vote
    st = E.State("n1", c, term=1)
    assert not E.grant_vote(st, {"term": 2, "candidate_id": "x1"}, 0)["granted"]     # x1 cannot stand


def test_heartbeat_with_newer_term_fences_a_primary():
    st = E.State("n1", cfg_of(3), term=3)
    E.become_primary(st, 0)
    E.receive_heartbeat_reply(st, "n2", {"term": 4, "ok": False}, 1)
    assert st.role == E.SECONDARY and st.term == 4


def test_priority_staggers_timeouts():
    c = cfg_of(3)
    rng = random.Random(1)
    t1 = [E.election_timeout_for(c, "n1", rng) for _ in range(50)]
    t3 = [E.election_timeout_for(c, "n3", rng) for _ in range(50)]
    assert max(t1) < min(t3)


# ───────────────────────────── simulator ─────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class MemStore:
    def __init__(self):
        self.doc, self.elections = None, []

    def load(self):
        return copy.deepcopy(self.doc)

    def save(self, doc):
        self.doc = copy.deepcopy(doc)

    def record_election(self, ev):
        self.elections.append(ev)


class Net:
    def __init__(self):
        self.nodes, self.down, self.cut = {}, set(), set()

    def link(self, a, b):
        return a not in self.down and b not in self.down and frozenset((a, b)) not in self.cut

    def partition(self, *groups):
        self.cut = set()
        ids = list(self.nodes)
        side = {n: i for i, g in enumerate(groups) for n in g}
        for a in ids:
            for b in ids:
                if a < b and side.get(a, -1) != side.get(b, -2):
                    self.cut.add(frozenset((a, b)))

    def heal(self):
        self.cut = set()


class SimTransport:
    def __init__(self, net, me):
        self.net, self.me = net, me

    def send(self, member, rpc, payload, timeout):
        to = member.node_id
        if to not in self.net.nodes or not self.net.link(self.me, to):
            return None
        node = self.net.nodes[to]
        return {"heartbeat": node.on_heartbeat, "vote": node.on_vote,
                "timeout_now": node.on_timeout_now}[rpc](copy.deepcopy(payload))


class Sim:
    def __init__(self, n_voters, n_nonvoting=0, seed=0):
        self.clock, self.net = Clock(), Net()
        self.rng = random.Random(seed)
        self.cfg = cfg_of(n_voters, n_nonvoting)
        self.stores = {}
        for m in self.cfg.members:
            self.stores[m.node_id] = MemStore()
            self.stores[m.node_id].save({"term": 0, "config": self.cfg.to_dict()})
            self._boot(m.node_id)
        self.next_tick = {nid: self.clock.t for nid in self.net.nodes}
        self.primaries_by_term = {}

    def _boot(self, nid):
        self.net.nodes[nid] = Node(nid, self.stores[nid], SimTransport(self.net, nid), clock=self.clock,
                                   rng=random.Random(self.rng.random()), parallel=False)

    def crash(self, nid):
        self.net.down.add(nid)

    def restart(self, nid):
        self.net.down.discard(nid)
        self._boot(nid)

    def check(self):
        for nid, node in self.net.nodes.items():
            if nid in self.net.down:
                continue
            if node.st.role == E.PRIMARY:
                other = self.primaries_by_term.setdefault(node.st.term, nid)
                assert other == nid, f"two primaries in term {node.st.term}: {other} and {nid}"

    def run(self, seconds, step=0.5):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            for nid, node in list(self.net.nodes.items()):
                if nid in self.net.down:
                    continue
                if self.clock.t >= self.next_tick[nid]:
                    node.round()
                    self.next_tick[nid] = self.clock.t + node.st.config.heartbeat_s
                self.check()

    def primaries(self):
        return sorted(n for n, node in self.net.nodes.items()
                      if n not in self.net.down and node.st.role == E.PRIMARY)


def test_three_members_elect_exactly_one_primary():
    s = Sim(3)
    s.run(30)
    assert s.primaries() == ["n1"]           # best priority stands first


def test_primary_crash_fails_over_to_best_survivor_and_old_primary_rejoins():
    s = Sim(3, seed=3)
    s.run(30)
    old_term = s.net.nodes["n1"].st.term
    s.crash("n1")
    s.run(25)
    assert s.primaries() == ["n2"]
    assert s.net.nodes["n2"].st.term > old_term
    ev = s.stores["n2"].elections[-1]
    assert ev["winner"] == "n2" and ev["votes_for"] == 2 and "n1" in ev["reason"]
    s.restart("n1")
    s.run(20)
    assert s.primaries() == ["n2"]            # no automatic failback
    assert s.net.nodes["n1"].st.role == E.SECONDARY


def test_minority_primary_steps_down_and_majority_elects():
    s = Sim(3, seed=5)
    s.run(30)
    s.net.partition(["n1"], ["n2", "n3"])
    s.run(40)
    assert s.net.nodes["n1"].st.role == E.SECONDARY   # CheckQuorum
    assert s.primaries() == ["n2"]
    s.net.heal()
    s.run(20)
    assert s.primaries() == ["n2"]


def test_isolated_secondary_does_not_inflate_the_term():
    s = Sim(3, seed=7)
    s.run(30)
    t = s.net.nodes["n1"].st.term
    s.net.partition(["n1", "n2"], ["n3"])
    s.run(120)
    assert s.net.nodes["n3"].st.term == t      # pre-vote failed every time
    s.net.heal()
    s.run(20)
    assert s.primaries() == ["n1"] and s.net.nodes["n1"].st.term == t


def test_even_member_count_never_fails_over_automatically():
    s = Sim(4, seed=2)
    # Even count: nobody becomes primary on their own. Seed n1 as primary via a takeover.
    s.net.nodes["n1"].takeover("operator")
    s.run(20)
    assert s.primaries() == ["n1"]
    s.crash("n1")
    s.run(60)
    assert s.primaries() == []                 # manual mode: no election
    r = s.net.nodes["n2"].takeover("manish")
    assert r["ok"] and s.primaries() == ["n2"]
    s.restart("n1")                            # manual mode: n1 resumes as primary from disk …
    s.run(10)
    assert s.primaries() == ["n2"]             # … and is fenced by n2's higher term at first contact


def test_even_split_elects_nobody():
    s = Sim(4, seed=9)
    s.net.nodes["n1"].takeover("operator")
    s.run(10)
    s.net.partition(["n1", "n2"], ["n3", "n4"])
    s.run(60)
    assert s.primaries() == ["n1"]             # manual mode keeps the existing primary
    with pytest.raises(PermissionError):
        s.net.nodes["n1"].transfer("n3")       # n3 is not answering


def test_takeover_refused_in_automatic_mode():
    s = Sim(3)
    s.run(30)
    with pytest.raises(PermissionError):
        s.net.nodes["n2"].takeover("x")


def test_non_voting_member_never_becomes_primary_and_does_not_count():
    s = Sim(3, n_nonvoting=1, seed=4)
    s.run(30)
    s.crash("n1")
    s.crash("n2")
    s.run(60)
    assert s.primaries() == []                 # 1 of 3 voters left; x1 does not help
    assert s.net.nodes["x1"].st.role == E.SECONDARY


def test_planned_transfer_in_automatic_mode():
    s = Sim(3, seed=11)
    s.run(30)
    r = s.net.nodes["n1"].transfer("n3")
    assert r["ok"]
    s.run(10)
    assert s.primaries() == ["n3"]
    assert s.stores["n3"].elections[-1]["winner"] == "n3"


def test_step_down_picks_best_healthy_successor():
    s = Sim(3, seed=12)
    s.run(30)
    r = s.net.nodes["n1"].step_down()
    assert r["ok"] and r["to"] == "n2"
    s.run(30)
    assert s.primaries() == ["n2"]


def test_vote_survives_restart():
    s = Sim(3)
    node = s.net.nodes["n3"]
    node.on_vote({"term": 50, "candidate_id": "n1"})
    s.restart("n3")
    r = s.net.nodes["n3"].on_vote({"term": 50, "candidate_id": "n2"})
    assert not r["granted"]


def test_adding_a_fourth_voter_switches_to_manual_and_a_fifth_restores_automatic():
    s = Sim(3, seed=13)
    s.run(30)
    p = s.net.nodes["n1"]
    c4 = p.change_config(lambda c: E.add_member(c, E.Member("n4", "n4", priority=4, votes=1)))
    assert E.failover_mode(c4)[0] == E.MANUAL
    c5 = p.change_config(lambda c: E.add_member(c, E.Member("n5", "n5", priority=5, votes=1)))
    assert E.failover_mode(c5)[0] == E.AUTOMATIC


@pytest.mark.parametrize("seed", range(25))
def test_random_faults_never_produce_two_primaries_and_recover(seed):
    s = Sim(5, seed=seed)
    rng = random.Random(seed)
    s.run(30)
    for _ in range(12):
        ids = list(s.net.nodes)
        k = rng.random()
        if k < 0.35:
            rng.shuffle(ids)
            cut = rng.randint(1, 4)
            s.net.partition(ids[:cut], ids[cut:])
        elif k < 0.6:
            s.crash(rng.choice(ids))
        elif k < 0.8:
            for d in list(s.net.down):
                s.restart(d)
        else:
            s.net.heal()
        s.run(rng.uniform(3, 25))
    for d in list(s.net.down):
        s.restart(d)
    s.net.heal()
    s.run(60)
    assert len(s.primaries()) == 1             # liveness once everything is connected again
