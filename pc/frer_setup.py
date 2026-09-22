"""Bidirectional FRER (802.1CB) between the two Kontron D10s, done properly.

Protects all three boards <-> PC, both directions, over the two D10s' parallel
links (Gi1/1 + Gi1/2), with spanning tree off on both ring ports -- FRER alone provides the redundancy, which is the point: there is no
detection delay and no reconvergence, so a cut costs zero frames.

Everything here was learned the hard way against the real rig. The six
things that actually matter, none of which are obvious from the field
names alone:

1. The per-port VCL "Matching" mode (vcl.config.interface.vcl_cfg) decides
   what the hardware TCAM key contains. On `dmac_dip` -- the mode every
   port here needs -- the key holds the DESTINATION MAC and nothing about
   the source. A stream that asks to match source AND destination can
   therefore never fire on such a port: the source simply isn't in the
   lookup key. This silently cost most of a day; the symptom is a
   perfectly clean config (frerClientAttached true, zero warnings) with
   every FRER counter pinned at zero. Generation streams below match on
   destination MAC only, for exactly this reason.

2. ...but a destination-MAC-only stream on the RECOVERY side is dangerous:
   the recovery classifier sits on the ring ports, where the far switch's
   own management traffic to the PC also arrives, carrying the same
   destination MAC. Matched, it gets pulled into the FRER pipeline and
   eaten -- which cut switch2 off the network entirely the one time it was
   tried. The recovery streams therefore match destination MAC AND the
   FRER VLAN tag. Management traffic is untagged, so it can't collide.

3. For (2) to hold, the FRER frames must actually arrive tagged -- on BOTH
   paths. They don't by default: a ring port whose native VLAN happens to
   be the FRER VLAN egresses those frames untagged, so one of the two
   copies silently fails the recovery match while the other passes. Hence
   HybridEgressTagging=tagAll on the ring ports.

4. The mirror of (3) at the edges: the PC's NIC and the boards have no
   VLAN 200 sub-interface, so anything still tagged when it reaches them
   is dropped by the host, not the switch. Recovery's Terminate strips the
   R-tag but not the VLAN tag -- hence untagAll on every end-host port.

5. Replication itself is the chip's own, via FLOODING inside the FRER VLAN
   -- there is no separate "fan out to EgressPorts" path. An earlier
   attempt to fan out with multi-port static FDB entries instead does
   deliver two copies, but it cannot work bidirectionally: the entry that
   fans direction A out (dst=PC -> both ring ports on switch2) is the same
   MAC that direction B's frames arrive FROM on those ports, and the
   switch then drops them. Both directions broke each other, symmetrically,
   until the static entries were removed entirely.

   Flooding is also why an early attempt looked like a storm: back then
   classification was broken (see 1), so nothing was ever recovered and
   frames just circulated. With recovery actually consuming them
   (Terminate strips the R-tag and forwards to one egress port), a flooded
   frame is absorbed at the far switch instead of being passed on.

   MAC learning is disabled in the FRER VLAN on purpose: if the far side
   learns where a MAC lives, the next frame is unicast to one port and the
   fan-out silently degrades to a single path -- which the reverse
   direction's own traffic would otherwise teach it almost immediately.

6. One recovery instance can only follow ONE sequence counter. Pointing
   several generation instances at a single recovery instance interleaves
   their independent R-tag sequences, and the recovery engine reports the
   gaps as Lost -- 765 of them in one run, while ping showed 0% loss.

   This is what shapes the VLAN plan. PC -> board is safe to share one FRER
   VLAN, because each board is a different destination MAC and so gets its
   own recovery stream and instance. board -> PC is not: every board sends
   to the same PC MAC, so one shared recovery stream would match all three.
   Each board's upstream flow therefore gets its own FRER VLAN, which is
   what the recovery stream matches on.

   The same shape appears in miniature if HistoryLen is too tight for two
   paths with different delays: at 8 it invented losses, at 32 (with
   ResetTimeoutMsec 1000) Lost stays flat at 0.

Run this with Gi1/2 unplugged if you want zero risk while it applies;
nothing here needs the second path to be live in order to be configured.
"""
import json
import sys
import urllib.request

SWITCH1 = "http://192.168.100.1/json_rpc"  # PC side
SWITCH2 = "http://192.168.100.2/json_rpc"  # ESP32 side

# Downstream (PC -> board) all shares one FRER VLAN: each board has a distinct
# destination MAC, so each gets its own recovery stream and therefore its own
# recovery instance. Upstream (board -> PC) cannot share, because every board
# sends to the SAME destination MAC -- one recovery stream would match all
# three and feed three interleaved sequence counters into one instance, which
# reads out as a huge and completely fake Lost count (note 6). So upstream gets
# one FRER VLAN per board, and the recovery stream tells them apart by VLAN.
DOWN_VLAN = 200
RING_PORTS = ["Gi 1/1", "Gi 1/2"]

PC_MAC = "d4:5d:64:b2:5d:c3"
PC_PORT_SW1 = "Gi 1/6"                     # PC hangs off switch1 here


class Board:
    """One edge board and the two protected flows that terminate on it.

    Ports and MACs are verified against mac.status.fdb.full.get, NOT guessed
    from RX-counter deltas -- an early guess of the wrong port is what made
    direction A look broken for hours.

    Stream and instance ids are pinned per board rather than derived, so that
    re-running this does not renumber (and thus orphan) objects that are
    already live on the switches.
    """

    def __init__(self, name, mac, port, up_vlan, down_inst, up_inst,
                 down_gen, down_rec, up_gen, up_rec):
        self.name, self.mac, self.port, self.up_vlan = name, mac, port, up_vlan
        self.down_inst, self.up_inst = down_inst, up_inst
        self.down_gen, self.down_rec = down_gen, down_rec
        self.up_gen, self.up_rec = up_gen, up_rec


#                 name       mac                  port      up   inst d/u  streams dg/dr/ug/ur
BOARDS = [
    Board("esp32-1", "a6:cb:8f:e7:f0:bd", "Gi 1/3", 200,  2,  1, 12, 13, 10, 11),
    Board("esp32-2", "2a:84:85:80:9b:85", "Gi 1/6", 201, 10, 11, 14, 15, 16, 17),
    Board("esp32-3", "a6:cb:8f:e9:88:45", "Gi 1/5", 202, 12, 13, 18, 19, 20, 21),
]

ALL_VLANS = sorted({DOWN_VLAN} | {b.up_vlan for b in BOARDS})

# Instances 3-6 and VLAN 35 on these switches belong to an unrelated older
# project. Nothing here touches them; the ids above stay clear of them.

_openers = {}


def _opener(url):
    if url not in _openers:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, "admin", "")
        _openers[url] = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
    return _openers[url]


def rpc(url, method, params=None, quiet=False):
    body = json.dumps({"method": method, "params": params if params is not None else [], "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with _opener(url).open(req, timeout=6) as r:
        resp = json.loads(r.read())
    if resp.get("error"):
        if quiet:
            return None
        raise RuntimeError(f"{method} @ {url} -> {resp['error']}")
    return resp["result"]


def stream_conf(dmac, vlan=None):
    """Destination-MAC match, optionally also requiring the FRER VLAN tag.

    Source MAC is deliberately left as don't-care: on a dmac_dip port it is
    not part of the TCAM key at all, and requiring it means the rule never
    matches (note 1).
    """
    return {
        "MulticastDMac": "any", "BroadcastDMac": "any",
        "destinationMacAddress": dmac, "destinationMacMask": "ff:ff:ff:ff:ff:ff",
        "sourceMacAddress": "00:00:00:00:00:00", "sourceMacMask": "00:00:00:00:00:00",
        "outerTag": "one" if vlan else "any", "outerTagIsSTag": "any",
        "outerTagVidValue": vlan or 0, "outerTagVidMask": 4095 if vlan else 0,
        "outerTagPcpValue": 0, "outerTagPcpMask": 0, "outerTagDei": "any",
        "innerTag": "any", "innerTagIsSTag": "any", "innerTagVidValue": 0, "innerTagVidMask": 0,
        "innerTagPcpValue": 0, "innerTagPcpMask": 0, "innerTagDei": "any",
        "protocol": "ANY",  # upper-case; "any" is rejected
    }


def frer_conf(mode, stream_id, egress_ports, vlan):
    conf = {
        "Mode": mode,                       # lower-case per /json_spec
        "FrerVlan": vlan, "EgressPorts": egress_ports,
        # HistoryLen 8 is too tight for two paths whose copies interleave: the
        # vector algorithm then reports sequence gaps as Lost even though no
        # frame was actually lost (ping stayed at 0% while Lost climbed).
        # 32 (the maximum) makes Lost read 0, matching reality.
        "Algorithm": "vector", "HistoryLen": 32, "ResetTimeoutMsec": 1000,
        "TakeNoSequence": False, "IndividualRecovery": False,
        # Terminate strips the R-tag on the way out -- right for recovery
        # (the end system has no idea what an R-tag is), wrong for
        # generation, whose whole job is to add one.
        "Terminate": mode == "recovery",
        "LaErrDetection": False, "LaErrDifference": 100, "LaErrPeriodMsec": 2000,
        "LaErrPaths": 2, "LaErrResetPeriodMsec": 30000,
        "AdminActive": True,
    }
    for i in range(8):
        conf[f"StreamId{i}"] = stream_id if i == 0 else 0   # unused slots are 0, not -1
    return conf


def put_stream(url, sid, conf):
    rpc(url, "vcl.config.stream.add", [sid, conf], quiet=True)
    rpc(url, "vcl.config.stream.set", [sid, conf])


def attach_stream(url, port, sid):
    # Re-attach rather than assume: a classifier attached BEFORE the port's
    # matching mode was corrected stays programmed with the old key.
    rpc(url, "vcl.config.interface.stream.del", [port, sid], quiet=True)
    rpc(url, "vcl.config.interface.stream.add", [port, sid])


def put_frer(url, inst, conf):
    rpc(url, "frer.config.add", [inst, conf], quiet=True)
    rpc(url, "frer.config.set", [inst, conf])


def set_matching(url, port, mode="dmac_dip"):
    cur = rpc(url, "vcl.config.interface.vcl_cfg.get", [port])
    if cur["Matching"] != mode:
        rpc(url, "vcl.config.interface.vcl_cfg.set", [port, {"Matching": mode}])


def set_vlan(url, port, add_vlans, egress_tagging, native=None, replace=False):
    cfg = rpc(url, "vlan.config.interface.get", [port])
    cfg["Mode"] = "hybrid"
    # replace=True for the board ports, which ship as members of all 4095 VLANs;
    # left alone they receive the flood of every other board's FRER VLAN too.
    cfg["HybridVlans"] = sorted(add_vlans) if replace else sorted(set(cfg["HybridVlans"]) | set(add_vlans))
    cfg["HybridEgressTagging"] = egress_tagging
    if native is not None:
        cfg["HybridNativeVlan"] = native
    rpc(url, "vlan.config.interface.set", [port, cfg])


def disable_stp(url, port):
    cfg = rpc(url, "mstp.config.cist.interface.get", [port])
    if cfg["Enable"]:
        cfg["Enable"] = False
        rpc(url, "mstp.config.cist.interface.set", [port, cfg])



# ---------------------------------------------------------------------------
def main():
    print(f"--- ring ports: VLANs {ALL_VLANS} tagged, dmac_dip, STP off ---")
    for sw in (SWITCH1, SWITCH2):
        for p in RING_PORTS:
            set_matching(sw, p)
            set_vlan(sw, p, ALL_VLANS, "tagAll", native=1)   # note 3; native 1 keeps
            # untagged loop-protection probes out of the FRER VLANs, where they
            # would otherwise flood across both links and trip loop protection.
            disable_stp(sw, p)

    print("--- PC port: member of every FRER VLAN, untagged egress (note 4) ---")
    set_matching(SWITCH1, PC_PORT_SW1)
    set_vlan(SWITCH1, PC_PORT_SW1, [1] + ALL_VLANS, "untagAll")

    print("--- board ports ---")
    for b in BOARDS:
        set_matching(SWITCH2, b.port)
        # Only its own two VLANs: these ports ship as members of all 4095, which
        # would hand every board the flood of every other board's FRER VLAN.
        set_vlan(SWITCH2, b.port, [1, DOWN_VLAN, b.up_vlan], "untagAll", replace=True)
        print(f"  {b.name:8} {b.port}  down=vlan{DOWN_VLAN}  up=vlan{b.up_vlan}")

    print(f"--- VLANs {ALL_VLANS}: flooding ON (it IS the fan-out), learning OFF (note 5) ---")
    for sw in (SWITCH1, SWITCH2):
        for v in ALL_VLANS:
            rpc(sw, "vlan.config.global.flooding.set", [v, True])
            rpc(sw, "mac.config.vlan.learn.set", [v, {"Mode": False}])

    for b in BOARDS:
        print(f"\n--- {b.name} ---")

        # PC -> board: generate at switch1 on the PC port, recover at switch2
        # on the board's port. Told apart from the other boards by DMAC.
        # Recovery goes in FIRST, every time. A generation instance whose
        # recovery counterpart does not exist yet floods a FRER VLAN that
        # nothing absorbs, and the far switch floods it straight back across
        # the ring -- the storm from note 5, in a window of a few hundred ms.
        print(f"  PC -> {b.name}: vlan{DOWN_VLAN} inst{b.down_inst}")
        put_stream(SWITCH2, b.down_rec, stream_conf(b.mac, DOWN_VLAN))       # rec: DMAC + VLAN
        for p in RING_PORTS:
            attach_stream(SWITCH2, p, b.down_rec)
        put_frer(SWITCH2, b.down_inst,
                 frer_conf("recovery", b.down_rec, [b.port], DOWN_VLAN))

        put_stream(SWITCH1, b.down_gen, stream_conf(b.mac))                  # gen: DMAC only
        attach_stream(SWITCH1, PC_PORT_SW1, b.down_gen)
        put_frer(SWITCH1, b.down_inst,
                 frer_conf("generation", b.down_gen, RING_PORTS, DOWN_VLAN))

        # board -> PC: generate at switch2 on the board's port into that
        # board's own FRER VLAN, recover at switch1 on the PC port. Told apart
        # from the other boards by VLAN, since the DMAC is the PC either way.
        print(f"  {b.name} -> PC: vlan{b.up_vlan} inst{b.up_inst}")
        put_stream(SWITCH1, b.up_rec, stream_conf(PC_MAC, b.up_vlan))
        for p in RING_PORTS:
            attach_stream(SWITCH1, p, b.up_rec)
        put_frer(SWITCH1, b.up_inst,
                 frer_conf("recovery", b.up_rec, [PC_PORT_SW1], b.up_vlan))

        put_stream(SWITCH2, b.up_gen, stream_conf(PC_MAC))
        attach_stream(SWITCH2, b.port, b.up_gen)
        put_frer(SWITCH2, b.up_inst,
                 frer_conf("generation", b.up_gen, RING_PORTS, b.up_vlan))

    # Single-port delivery entries on each recovery switch (note 5). Learning is
    # off in these VLANs, so without them the recovery switch does not know
    # where the destination lives and floods the recovered frame straight back
    # across the ring -- a real broadcast storm, ~3M pkt/s/port, not a
    # theoretical one. One port each: a multi-port entry here breaks the
    # opposite direction, whose frames arrive *from* those same ring ports.
    print("\n--- static delivery entries (note 5) ---")
    for b in BOARDS:
        rpc(SWITCH2, "mac.config.fdb.static.add", [DOWN_VLAN, b.mac, {"PortList": [b.port]}])
        rpc(SWITCH1, "mac.config.fdb.static.add", [b.up_vlan, PC_MAC, {"PortList": [PC_PORT_SW1]}])

    print("\n--- status ---")
    for b in BOARDS:
        for sw, name, inst in ((SWITCH1, "switch1", b.down_inst), (SWITCH2, "switch2", b.down_inst),
                               (SWITCH2, "switch2", b.up_inst), (SWITCH1, "switch1", b.up_inst)):
            st = rpc(sw, "frer.status.get", [inst], quiet=True)
            if not st:
                print(f"  {name} inst{inst}: MISSING")
                continue
            warns = [k for k, v in st.items()
                     if k.startswith("Warning") and k != "WarningNone" and v]
            print(f"  {b.name:8} {name} inst{inst:<3} {st['OperState']}, {warns or 'no warnings'}")

    print("\nSave to startup when happy:")
    print("  icfg.control.copy.set runningConfig -> startupConfig")


if __name__ == "__main__":
    main()
