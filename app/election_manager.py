"""Runs the pure election logic in `app.election` for one node: persistence, timers, and the
RPCs to the other members. Transport and storage are injected, so the same `Node` class runs
in production (HTTP + MongoDB) and in the partition simulator in tests/test_election_sim.py.

Locking: every read or write of `self.st` happens under `self.lock`. Network calls are made
OUTSIDE the lock; their results are folded back in under it, and every multi-step operation
(an election, a transfer) re-checks the term after each network phase so a concurrent change
aborts it instead of being overwritten.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Protocol

from app import election as E

log = logging.getLogger("app.election")


class Transport(Protocol):
    def send(self, member: E.Member, rpc: str, payload: dict, timeout: float) -> Optional[dict]:
        """Deliver an RPC; return the reply, or None when the member could not be reached."""


class Store(Protocol):
    def load(self) -> Optional[dict]: ...
    def save(self, doc: dict) -> None: ...
    def record_election(self, ev: dict) -> None: ...


@dataclass
class PeerView:
    last_reply: Optional[float] = None
    beats: deque = field(default_factory=lambda: deque(maxlen=16))
    info: dict = field(default_factory=dict)
    role: Optional[str] = None
    term: int = 0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Node:
    def __init__(self, node_id: str, store: Store, transport: Transport, *,
                 clock: Callable[[], float] = time.monotonic,
                 rng: Optional[random.Random] = None,
                 info: Callable[[], dict] = lambda: {},
                 parallel: bool = True):
        self.node_id = node_id
        self.store = store
        self.transport = transport
        self.clock = clock
        self.rng = rng or random.Random()
        self.info = info
        self.lock = threading.RLock()
        self.peers: Dict[str, PeerView] = {}
        self._pool = ThreadPoolExecutor(max_workers=8) if parallel else None
        self.st = self._load()
        self.deadline = self.clock() + E.election_timeout_for(self.st.config, node_id, self.rng)

    # ───────────── persistence ─────────────

    def _load(self) -> E.State:
        doc = self.store.load() or {}
        cfg = E.Config.from_dict(doc.get("config")) if doc.get("config") else E.Config(
            members=[E.Member(node_id=self.node_id, priority=1, votes=1)])
        st = E.State(node_id=self.node_id, config=cfg, term=int(doc.get("term", 0)),
                     voted_for=doc.get("voted_for"))
        mode, _ = E.failover_mode(cfg)
        # Restart rules: automatic mode always restarts as a secondary (a fresh election
        # decides); manual mode keeps a primary's role so an even-sized set is not left without
        # one after a reboot (a takeover elsewhere raised the term and will fence it on contact).
        if mode == E.MANUAL and doc.get("role") == E.PRIMARY:
            E.become_primary(st, self.clock())
        return st

    def _persist(self) -> None:
        """Called under the lock BEFORE any reply that depends on term/vote leaves the node."""
        st = self.st
        self.store.save({"term": st.term, "voted_for": st.voted_for, "role": st.role,
                         "leader_id": st.leader_id, "config": st.config.to_dict(),
                         "updated_at": _iso_now()})

    def _record(self, winner: str, votes_for: int, voters: int, reason: str) -> None:
        ev = {"term": self.st.term, "winner": winner, "votes_for": votes_for, "voters": voters,
              "reason": reason, "at": _iso_now()}
        try:
            self.store.record_election(ev)
        except Exception as e:  # noqa: BLE001 — the history is informational
            log.warning("election[%s]: could not record election: %s", self.node_id, e)
        log.info("ELECTION term=%s winner=%s votes=%s/%s reason=%s", ev["term"], winner, votes_for, voters, reason)

    def _reset_deadline(self, now: float) -> None:
        self.deadline = now + E.election_timeout_for(self.st.config, self.node_id, self.rng)

    # ───────────── fan-out ─────────────

    def _broadcast(self, members: List[E.Member], rpc: str, payload: dict, timeout: float) -> Dict[str, Optional[dict]]:
        def one(m):
            try:
                return m.node_id, self.transport.send(m, rpc, payload, timeout)
            except Exception:  # noqa: BLE001 — unreachable is a normal answer here
                return m.node_id, None
        if self._pool is None:
            return dict(one(m) for m in members)
        return dict(self._pool.map(one, members))

    def _others(self) -> List[E.Member]:
        return [m for m in self.st.config.members if m.node_id != self.node_id]

    # ───────────── RPC handlers (server side) ─────────────

    def on_heartbeat(self, msg: dict) -> dict:
        with self.lock:
            now = self.clock()
            before = (self.st.term, self.st.voted_for, self.st.role, self.st.config.version)
            reply = E.receive_heartbeat(self.st, msg, now)
            sender = msg.get("sender_id")
            if sender and reply.get("ok"):
                pv = self.peers.setdefault(sender, PeerView())
                pv.last_reply, pv.role, pv.term = now, msg.get("role"), int(msg.get("term", 0))
                if msg.get("info"):
                    pv.info = msg["info"]
            if msg.get("role") == E.PRIMARY and reply.get("ok"):
                self._reset_deadline(now)
            if before != (self.st.term, self.st.voted_for, self.st.role, self.st.config.version):
                self._persist()
            reply["info"] = self.info()
            return reply

    def on_vote(self, req: dict) -> dict:
        with self.lock:
            now = self.clock()
            if req.get("pre_vote"):
                return E.answer_pre_vote(self.st, req, now)
            reply = E.grant_vote(self.st, req, now)
            self._persist()                     # term + vote are durable before the reply
            if reply["granted"]:
                self._reset_deadline(now)
            return reply

    def on_timeout_now(self, req: dict) -> dict:
        """The primary asks this member to stand immediately (planned transfer)."""
        with self.lock:
            if int(req.get("term", 0)) < self.st.term:
                return {"ok": False, "term": self.st.term, "reason": "stale term"}
            mode, _ = E.failover_mode(self.st.config)
            me = self.st.me
            if me is None or not me.electable:
                return {"ok": False, "term": self.st.term, "reason": "cannot become primary"}
        if mode == E.AUTOMATIC:
            won = self.run_election(transfer=True, reason=req.get("reason") or "planned transfer")
            return {"ok": won, "term": self.st.term}
        with self.lock:                         # manual: the primary hands over directly
            self.st.adopt_term(max(self.st.term, int(req.get("term", 0))) + 1)
            E.become_primary(self.st, self.clock())
            self._persist()
            self._record(self.node_id, 1, len(self.st.config.voters), req.get("reason") or "handed over")
            return {"ok": True, "term": self.st.term}

    # ───────────── the round ─────────────

    def round(self) -> str:
        """One timer tick. Returns the action taken (for logs and tests)."""
        with self.lock:
            now = self.clock()
            action = E.tick(self.st, now, self.deadline)
            if action == "become_primary":
                self.st.adopt_term(self.st.term + 1)
                E.become_primary(self.st, now)
                self._persist()
                self._record(self.node_id, 1, 1, "standalone")
                action = "heartbeat"
            elif action == "step_down":
                log.warning("election[%s]: no reply from a majority of voters for %ss — stepping down",
                            self.node_id, self.st.config.election_timeout_s)
                E.step_down(self.st, now, hold_s=0)
                self._reset_deadline(now)
                self._persist()
                return "step_down"
        if action == "heartbeat":
            self.send_heartbeats()
        elif action == "start_election":
            self.run_election()
        else:
            self.send_heartbeats()               # secondaries also heartbeat, for peer health
        return action

    def send_heartbeats(self) -> None:
        with self.lock:
            msg = E.heartbeat_message(self.st)
            msg["info"] = self.info()
            term = self.st.term
            others = self._others()
            timeout = max(1.0, self.st.config.heartbeat_s * 0.9)
        replies = self._broadcast(others, "heartbeat", msg, timeout)
        with self.lock:
            now = self.clock()
            changed = False
            for nid, rep in replies.items():
                pv = self.peers.setdefault(nid, PeerView())
                ok = bool(rep and rep.get("ok"))
                pv.beats.append(ok)
                if rep:
                    pv.last_reply = now if ok else pv.last_reply
                    pv.role, pv.term = rep.get("role"), int(rep.get("term", 0))
                    if rep.get("info"):
                        pv.info = rep["info"]
                if self.st.role == E.PRIMARY and self.st.term == term:
                    before = self.st.term
                    E.receive_heartbeat_reply(self.st, nid, rep, now)
                    changed |= self.st.term != before
                elif rep and int(rep.get("term", 0)) > self.st.term:
                    changed |= self.st.adopt_term(int(rep["term"]))
            if changed:
                log.warning("election[%s]: saw a newer term — now secondary in term %s", self.node_id, self.st.term)
                self._reset_deadline(now)
                self._persist()

    def run_election(self, transfer: bool = False, reason: Optional[str] = None) -> bool:
        """Pre-vote (skipped for a transfer), then a real vote. Returns True when this node won."""
        with self.lock:
            st = self.st
            me = st.me
            if me is None or not me.electable or st.role == E.PRIMARY:
                return False
            voters = [m for m in st.config.voters if m.node_id != self.node_id]
            n_voters = len(st.config.voters)
            timeout = max(1.5, st.config.heartbeat_s)
            start_term = st.term
            lost_leader = st.leader_id
        if not transfer:
            pre = self._broadcast(voters, "vote", {"term": start_term + 1, "candidate_id": self.node_id,
                                                   "pre_vote": True}, timeout)
            with self.lock:
                got, need, highest = E.tally(self.st, pre)
                if highest > self.st.term:
                    self.st.adopt_term(highest)
                    self._persist()
                if got < need or self.st.term != start_term or self.st.role == E.PRIMARY:
                    self._reset_deadline(self.clock())
                    return False
        with self.lock:
            if self.st.term != start_term:
                return False
            self.st.adopt_term(start_term + 1)
            self.st.voted_for = self.node_id
            self.st.role = E.CANDIDATE
            my_term = self.st.term
            self._persist()                     # never ask for votes in a term we have not stored
        votes = self._broadcast(voters, "vote", {"term": my_term, "candidate_id": self.node_id,
                                                 "transfer": transfer}, timeout)
        with self.lock:
            now = self.clock()
            if self.st.term != my_term or self.st.role != E.CANDIDATE:
                return False                    # someone else moved the set on meanwhile
            got, need, highest = E.tally(self.st, votes)
            if highest > my_term:
                self.st.adopt_term(highest)
                self._reset_deadline(now)
                self._persist()
                return False
            if got >= need:
                E.become_primary(self.st, now)
                self._persist()
                why = reason or (f"{lost_leader} stopped answering" if lost_leader else "no primary")
                self._record(self.node_id, got, n_voters, why)
                won = True
            else:
                self.st.role = E.SECONDARY
                self._reset_deadline(now)
                self._persist()
                won = False
        if won:
            self.send_heartbeats()               # announce at once; others stop their timers
        return won

    # ───────────── operator actions ─────────────

    def _require_primary(self) -> None:
        if self.st.role != E.PRIMARY:
            raise PermissionError("only the primary can do this")

    def step_down(self) -> dict:
        """Primary gives up the role. In automatic mode the best healthy member is asked to
        stand at once; in manual mode the role must go to a named member (transfer)."""
        with self.lock:
            self._require_primary()
            mode, _ = E.failover_mode(self.st.config)
            if mode == E.STANDALONE:
                raise PermissionError("a standalone server cannot step down")
            target = self._best_successor()
        if target is None:
            raise PermissionError("no healthy member can take over")
        return self.transfer(target, reason=f"{self.node_id} stepped down")

    def _best_successor(self) -> Optional[str]:
        now = self.clock()
        ok = [m for m in self.st.config.members
              if m.node_id != self.node_id and m.electable
              and (self.peers.get(m.node_id) and self.peers[m.node_id].last_reply is not None
                   and now - self.peers[m.node_id].last_reply < self.st.config.election_timeout_s)]
        ok.sort(key=lambda m: (m.priority, m.node_id))
        return ok[0].node_id if ok else None

    def transfer(self, target_id: str, reason: Optional[str] = None) -> dict:
        with self.lock:
            self._require_primary()
            target = self.st.config.member(target_id)
            if target is None or not target.electable:
                raise ValueError("that member cannot become primary")
            pv = self.peers.get(target_id)
            now = self.clock()
            if not pv or pv.last_reply is None or now - pv.last_reply >= self.st.config.election_timeout_s:
                raise PermissionError(f"{target_id} is not answering")
            term = self.st.term
            # Step down first and hold off standing again, so the target wins cleanly.
            E.step_down(self.st, now, hold_s=self.st.config.election_timeout_s * 2)
            self._persist()
        rep = None
        try:
            rep = self.transport.send(target, "timeout_now",
                                      {"term": term, "reason": reason or f"handed over by {self.node_id}"},
                                      timeout=max(3.0, self.st.config.heartbeat_s * 3))
        except Exception:  # noqa: BLE001
            rep = None
        with self.lock:
            if rep and int(rep.get("term", 0)) > self.st.term:
                self.st.adopt_term(int(rep["term"]))
                self._persist()
            ok = bool(rep and rep.get("ok"))
            if not ok:
                log.warning("election[%s]: transfer to %s did not complete; normal elections continue",
                            self.node_id, target_id)
                self.st.stepped_down_until = 0.0     # let the regular election decide
            return {"ok": ok, "term": self.st.term, "to": target_id}

    def takeover(self, actor: str) -> dict:
        """Manual mode only: become primary in a new term. The old primary, if it is alive
        somewhere, sees the higher term on first contact and steps down."""
        with self.lock:
            mode, _ = E.failover_mode(self.st.config)
            if mode == E.AUTOMATIC:
                raise PermissionError("automatic failover is on; elections decide the primary")
            me = self.st.me
            if me is None or not me.electable:
                raise PermissionError("this server cannot become primary")
            if self.st.role == E.PRIMARY:
                raise PermissionError("already primary")
            known = max([self.st.term] + [pv.term for pv in self.peers.values()])
            self.st.adopt_term(known + 1)
            E.become_primary(self.st, self.clock())
            self._persist()
            self._record(self.node_id, 1, len(self.st.config.voters), f"manual takeover by {actor}")
        self.send_heartbeats()
        return {"ok": True, "term": self.st.term}

    def change_config(self, fn: Callable[[E.Config], E.Config]) -> E.Config:
        """Primary applies one membership change (one at a time, as in Raft's single-server
        changes) and pushes it with the next heartbeat."""
        with self.lock:
            self._require_primary()
            new = fn(self.st.config)
            if new.member(self.node_id) is None:
                raise ValueError("transfer the primary role before removing this server")
            self.st.config = new
            self._persist()
        self.send_heartbeats()
        return new

    # ───────────── read side ─────────────

    def is_primary(self) -> bool:
        with self.lock:
            return self.st.role == E.PRIMARY

    def leader_member(self) -> Optional[E.Member]:
        with self.lock:
            return self.st.config.member(self.st.leader_id) if self.st.leader_id else None

    def status(self) -> dict:
        with self.lock:
            st, now = self.st, self.clock()
            mode, reason = E.failover_mode(st.config)
            voters = st.config.voters
            timeout = st.config.election_timeout_s

            def healthy(nid):
                if nid == self.node_id:
                    return True
                pv = self.peers.get(nid)
                return bool(pv and pv.last_reply is not None and now - pv.last_reply < timeout)

            reachable = sum(1 for m in voters if healthy(m.node_id))
            members = []
            for m in st.config.members:
                pv = self.peers.get(m.node_id) or PeerView()
                is_self = m.node_id == self.node_id
                info = self.info() if is_self else pv.info
                role = st.role if is_self else (E.PRIMARY if st.leader_id == m.node_id else (pv.role or E.SECONDARY))
                members.append({
                    "node_id": m.node_id, "address": m.address, "priority": m.priority, "votes": m.votes,
                    "role": role if healthy(m.node_id) else "unreachable",
                    "healthy": healthy(m.node_id), "is_self": is_self,
                    "last_heartbeat_age_s": 0.0 if is_self else (round(now - pv.last_reply, 1) if pv.last_reply is not None else None),
                    "last_scan_at": info.get("last_scan_at"), "scan_overdue": bool(info.get("scan_overdue")),
                    "version": info.get("version"), "host": info.get("host") or {},
                    "heartbeats": [True] * 16 if is_self else list(pv.beats),
                })
            leader = st.config.member(st.leader_id) if st.leader_id else None
            return {
                "node_id": self.node_id, "role": st.role if mode != E.STANDALONE else (
                    "standalone" if st.role == E.PRIMARY else st.role),
                "term": st.term, "leader_id": st.leader_id,
                "leader_address": leader.address if leader else None,
                "mode": mode, "mode_reason": reason,
                "voting_members": len(voters), "quorum": E.quorum(len(voters)),
                "reachable_voters": reachable, "has_quorum": reachable >= E.quorum(len(voters)),
                "settings": {"heartbeat_interval_s": st.config.heartbeat_s,
                             "election_timeout_s": st.config.election_timeout_s},
                "config_version": st.config.version,
                "members": members,
            }
