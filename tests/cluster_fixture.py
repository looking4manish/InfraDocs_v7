"""Install an in-memory replica-set member as this process's cluster runtime, for API tests
that need a primary with reachable or unreachable secondaries. No network, no timers."""

from datetime import datetime, timezone

from app import cluster_runtime as RT
from app import election as E
from app.election_manager import Node, PeerView


class _Store:
    def __init__(self):
        self.doc, self.elections = None, []

    def load(self):
        return self.doc

    def save(self, doc):
        self.doc = doc

    def record_election(self, ev):
        self.elections.append(ev)


class _NoNet:
    def __init__(self):
        self.sent = []

    def send(self, member, rpc, payload, timeout):
        self.sent.append((member.node_id, rpc, payload))
        return None


def self_primary(node_id: str, address: str = "http://localhost:8090") -> Node:
    node = Node(node_id, _Store(), _NoNet(), parallel=False)
    node.st.config = E.Config(members=[E.Member(node_id, address, priority=1, votes=1)])
    node.st.term = 1
    E.become_primary(node.st, node.clock())
    RT._node = node
    return node


def add_secondary(node_id: str, seen: datetime, address: str = "http://n2:8081", votes: int = 1) -> None:
    node = RT._node
    cfg = node.st.config
    prio = E.next_priority(cfg) if votes else 0
    node.st.config = E.Config(members=cfg.members + [E.Member(node_id, address, priority=prio, votes=votes)],
                              heartbeat_s=cfg.heartbeat_s, election_timeout_s=cfg.election_timeout_s)
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    pv = PeerView()
    pv.last_reply = node.clock() - age
    node.peers[node_id] = pv


def reset() -> None:
    RT._node = None
