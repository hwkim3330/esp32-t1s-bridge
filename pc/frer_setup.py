# FRER (802.1CB) setup for the ESP32-1 -> PC direction, over the two D10s'
# dual link (Gi1/1 + Gi1/2 between 192.168.100.1 and .2).
#
# Recipe is d10-tsn-manager's validated one (commit 96ee0ae) + the AN1185
# manual sequence, not re-derived from scratch:
#   vcl.config.stream.add(id, {DMAC match, protocol:'ANY' -- must be
#       upper-case, "any" is rejected})
#   vcl.config.interface.stream.add(ingress_port, id)   # attaches the
#       classifier to an ingress port -- this *is* possible over plain
#       JSON-RPC, unlike the raw vcl_port_stream_config.htm "VCL MAC
#       matching" dropdown keti-reconfig's docs found no RPC for
#   frer.config.add(inst, {Mode, StreamId0=id, StreamId1..7=0 (NOT -1 --
#       WebStaX rejects -1 as invalid), EgressPorts, AdminActive, ...})
#
# First attempt used FrerVlan=1 -- the same VLAN carrying every other frame
# on this segment, flooding/learning fully on -- and matched on destination
# MAC alone. Microchip's own FRER doc (onlinedocs.microchip.com FRER
# chapter, and the BSP frer/vcap examples) is explicit that a FRER VLAN
# should be its own thing with flooding disabled ("Spanning Tree is used to
# avoid frames looping in other VLANs" / "the architecture prevents loop
# formation through disabled flooding in FRER VLANs") and that stream
# matching should stay narrow, not broad. We'd done neither: VLAN 1 kept
# flooding on, and DMAC-only matching also caught switch2's own management
# replies to the PC (same dst MAC). switch2 crashed outright once FRER went
# active. Both are fixed here: a dedicated VLAN 200 (flooding off) for the
# FRER-tagged transit hop, and source+destination MAC matching.
#
# One direction only (ESP32-1 -> PC): proves the mechanism with a single,
# clean before/after measurement rather than doubling the config surface
# on a first attempt. PC -> ESP32-1 is the same recipe mirrored (Generation
# at switch1, Recovery at switch2) once this direction is confirmed.
#
# STP is deliberately NOT touched by this script. Gi1/2 is physically
# unplugged right now (by design -- everything below is config-only until
# it's reconnected), so nothing here can go live until that happens.
import json
import sys
import urllib.request

FRER_VLAN = 200

SWITCH1 = "http://192.168.100.1/json_rpc"  # PC side (Gi1/6), FRER Recovery
SWITCH2 = "http://192.168.100.2/json_rpc"  # ESP32 side, FRER Generation

PC_MAC = "d4:5d:64:b2:5d:c3"
ESP32_1_MAC = "a6:cb:8f:e9:88:45"  # the board actually on Gi1/5 now. The
# original ESP32-1 unit (a6:cb:8f:e7:f0:bd) was swapped out here for a
# known-good third board (esp32-3) specifically to test cable vs. board --
# it landed on Gi1/3 instead, still can't be pinged even there, while
# esp32-3 works perfectly on Gi1/5 (the exact port/cable ESP32-1 used to
# occupy) -- conclusive: it was always the ESP32-1 *board* (its W5500 or
# related wiring), never the cable or the switch port. This name (and
# ESP32_1_INGRESS_ON_SWITCH2 below) is kept for the physical *role* in this
# topology ("the edge node on switch2"), not the specific unit -- direction
# A's classifier matches on MAC, so it transparently now protects whichever
# real board actually occupies that port.
ESP32_1_INGRESS_ON_SWITCH2 = "Gi 1/5"  # WRONG for most of this session's debugging: was
# guessed as "Gi 1/6" from RX counter deltas and never actually verified against the
# switch's own MAC table. mac.status.fdb.full.get finally settled it directly:
# A6:CB:8F:E7:F0:BD (ESP32-1) -> Gi 1/5; Gi 1/6 is actually ESP32-2's port
# (2A:84:85:80:9B:85, matches its ip-neigh MAC for 192.168.100.61). Every
# "Passed:0 / Gi1/2 TX stuck" symptom this whole debugging session -- surviving
# the VCL matching-mode fix, the VLAN/flooding fix, and even disabling STP on
# Gi1/2 outright -- makes sense in retrospect: the classifier was attached to a
# port ESP32-1's frames never actually arrive on, so it never fired at all, and
# what looked like "Generation not replicating" was just ordinary MAC-table
# forwarding of the unclassified original frame straight out Gi1/1.
STREAM_ID = 1
FRER_INST = 1
RING_PORTS = ["Gi 1/1", "Gi 1/2"]

_openers = {}


def _opener(url):
    if url not in _openers:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, "admin", "")
        _openers[url] = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
    return _openers[url]


def rpc(url, method, params=None):
    body = json.dumps({"method": method, "params": params if params is not None else [], "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with _opener(url).open(req, timeout=5) as r:
        resp = json.loads(r.read())
    if resp.get("error"):
        raise RuntimeError(f"{method}{params} @ {url} -> {resp['error']}")
    return resp["result"]


def stream_match_conf(dmac, smac="00:00:00:00:00:00"):
    # Matching on destination MAC alone also caught the switch's OWN
    # management-plane traffic to the PC (same dst MAC), which is the
    # leading suspect for why frer.config.add on switch2 crashed it last
    # time. Restricting source MAC to the actual talker (ESP32-1) as well
    # rules that out without touching STP/the ring at all.
    smac_mask = "00:00:00:00:00:00" if smac == "00:00:00:00:00:00" else "ff:ff:ff:ff:ff:ff"
    return {
        "MulticastDMac": "any", "BroadcastDMac": "any",
        "destinationMacAddress": dmac, "destinationMacMask": "ff:ff:ff:ff:ff:ff",
        "sourceMacAddress": smac, "sourceMacMask": smac_mask,
        "outerTag": "any", "outerTagIsSTag": "any",
        "outerTagVidValue": 0, "outerTagVidMask": 0,
        "outerTagPcpValue": 0, "outerTagPcpMask": 0, "outerTagDei": "any",
        "innerTag": "any", "innerTagIsSTag": "any", "innerTagVidValue": 0, "innerTagVidMask": 0,
        "innerTagPcpValue": 0, "innerTagPcpMask": 0, "innerTagDei": "any",
        "protocol": "ANY",
    }


def frer_conf(mode, stream_id, egress_ports):
    conf = {
        # Enum values are lower-case per /json_spec (vtss_appl_frer_mode_t:
        # "generation"/"recovery", mesa_frer_recovery_alg_t: "vector"/"match")
        # -- d10-tsn-manager's UI hid this behind a <select>, so the case
        # only shows up once you read the actual type spec.
        # FrerVlan=0 is rejected ("Invalid FRER VLAN") -- it wants a real
        # VLAN that exists on the switch. Use the dedicated, flooding-off
        # one (stage_frer_vlan), not VLAN 1.
        "Mode": mode, "FrerVlan": FRER_VLAN, "EgressPorts": egress_ports,
        "Algorithm": "vector", "HistoryLen": 8, "ResetTimeoutMsec": 100,
        "TakeNoSequence": False, "IndividualRecovery": False,
        # Terminate strips the R-tag before the frame egresses -- needed on
        # the Recovery side (switch1) since the PC is the actual end system
        # and its NIC has no reason to understand an R-tagged frame; wrong
        # on the Generation side, where the point is to ADD the R-tag.
        "Terminate": mode == "recovery",
        "LaErrDetection": False, "LaErrDifference": 100, "LaErrPeriodMsec": 2000,
        "LaErrPaths": 2, "LaErrResetPeriodMsec": 30000,
        "AdminActive": True,
    }
    for i in range(8):
        conf[f"StreamId{i}"] = stream_id if i == 0 else 0
    return conf


def stage_frer_vlan(url, vlan, ports):
    """Add `vlan` to each port's hybrid VLAN membership (so FRER-tagged
    frames can actually transit them) and disable flooding for that VLAN
    (so anything NOT explicitly stream-classified into it has nowhere to
    loop -- the mitigation the Microchip FRER docs actually call for)."""
    for port in ports:
        cfg = rpc(url, "vlan.config.interface.get", [port])
        changed = False
        if cfg["Mode"] != "hybrid":
            # Gi1/2 came back from the crash/reboot in "access" mode (only
            # its AccessVlan, 1, actually forwards) while Gi1/1 stayed
            # "hybrid" -- HybridVlans is silently ignored in access mode,
            # which is why VLAN 200 never showed real membership for it
            # despite being in the list. Caught by re-reading
            # vlan.status.membership.get after plugging the cable back in
            # and seeing only Gi1/1 as a member, not by inspection alone.
            cfg["Mode"] = "hybrid"
            changed = True
        if vlan not in cfg["HybridVlans"]:
            cfg["HybridVlans"] = sorted(set(cfg["HybridVlans"]) | {vlan})
            changed = True
        if changed:
            rpc(url, "vlan.config.interface.set", [port, cfg])
    rpc(url, "vlan.config.global.flooding.set", [vlan, False])


def create_stream(url, sid, conf):
    try:
        rpc(url, "vcl.config.stream.add", [sid, conf])
    except RuntimeError as e:
        print(f"  (add existed or failed, trying set: {e})")
    rpc(url, "vcl.config.stream.set", [sid, conf])


def create_frer(url, inst, conf):
    try:
        rpc(url, "frer.config.add", [inst, conf])
    except RuntimeError as e:
        print(f"  (frer add existed or failed, trying set: {e})")
        rpc(url, "frer.config.set", [inst, conf])


print(f"Stream {STREAM_ID}: src {ESP32_1_MAC} -> dst {PC_MAC} (ESP32-1 -> PC direction only)")
print(f"FRER VLAN {FRER_VLAN} (dedicated, flooding disabled) on {RING_PORTS} of both switches")

def fix_vcl_matching(url, port, mode="dmac_dip"):
    """Per-port VCL classification key type. Gi1/2 came up as "smac_sip"
    on both switches while Gi1/1 was "dmac_dip" -- our stream classifies
    on destination MAC, so a port keyed on source instead never matches
    the rule at the hardware lookup level, even though the classifier
    itself was correctly attached there. keti-reconfig's own docs called
    this "raw JSON-RPC can't set it, web-form only" -- vcl_cfg.set says
    otherwise."""
    cur = rpc(url, "vcl.config.interface.vcl_cfg.get", [port])
    if cur["Matching"] != mode:
        rpc(url, "vcl.config.interface.vcl_cfg.set", [port, {"Matching": mode}])
        print(f"  {url} {port}: Matching {cur['Matching']} -> {mode}")


print("\n--- fixing VCL matching mode on the ring ports (dmac_dip, to match our stream) ---")
for sw in (SWITCH1, SWITCH2):
    for p in RING_PORTS:
        fix_vcl_matching(sw, p)

print(f"\n--- staging VLAN {FRER_VLAN} on the ring ports + the end ports ---")
# Recovery's WarningVlanMembership kept firing even with both ring ports
# correctly in VLAN 200 -- it was checking the EGRESS port (Gi1/6 toward
# the PC) too, which the ring-only staging never touched. Same idea on
# switch2's side for its ESP32-1 ingress port.
stage_frer_vlan(SWITCH1, FRER_VLAN, RING_PORTS + ["Gi 1/6"])
stage_frer_vlan(SWITCH2, FRER_VLAN, RING_PORTS + [ESP32_1_INGRESS_ON_SWITCH2])

print(f"\n--- switch2 (Generation): ingress {ESP32_1_INGRESS_ON_SWITCH2} ---")
try:
    rpc(SWITCH2, "vcl.config.interface.stream.del", ["Gi 1/6", STREAM_ID])
    print("  (cleaned up stale stream1 attachment on Gi 1/6 -- that's ESP32-2's port, not ESP32-1's)")
except RuntimeError:
    pass
create_stream(SWITCH2, STREAM_ID, stream_match_conf(PC_MAC, ESP32_1_MAC))
rpc(SWITCH2, "vcl.config.interface.stream.add", [ESP32_1_INGRESS_ON_SWITCH2, STREAM_ID])
create_frer(SWITCH2, FRER_INST, frer_conf("generation", STREAM_ID, RING_PORTS))
gen_status = rpc(SWITCH2, "frer.status.get", [FRER_INST])
print("frer.status (switch2/Generation):", gen_status)

print(f"\n--- switch1 (Recovery): ingress {RING_PORTS}, egress toward PC (Gi 1/6) ---")
create_stream(SWITCH1, STREAM_ID, stream_match_conf(PC_MAC, ESP32_1_MAC))
for p in RING_PORTS:
    rpc(SWITCH1, "vcl.config.interface.stream.add", [p, STREAM_ID])
create_frer(SWITCH1, FRER_INST, frer_conf("recovery", STREAM_ID, ["Gi 1/6"]))
rec_status = rpc(SWITCH1, "frer.status.get", [FRER_INST])
print("frer.status (switch1/Recovery):", rec_status)

print("\n--- root cause of the zero-traffic mystery, found via mstp.status.interface.get ---")
# frer.statistics.get kept showing Passed:0 with an otherwise-perfect-looking
# config (vcl.status.stream.get confirmed frerClientAttached:true on both
# switches, vcl.config.interface.stream.get confirmed Gi1/6->stream1 on
# switch2). The missing piece wasn't FRER/VCL at all: mstp.status.interface.get
# showed Gi1/2 at PortState "discarding" / PortRole "AlternatePort" on both
# switches. STP port state gates ALL egress in hardware (except BPDUs) --
# including a FRER-replicated copy -- so Generation's second copy onto Gi1/2
# was being silently dropped downstream of FRER the whole time. Confirmed by
# Gi1/2's TX counter never moving even by one packet across many samples.
#
# mstp.config.cist.interface's "Enable" (per Microchip's own field
# description: "Control whether port is controlled by xSTP. If disabled,
# the port forwarding state follow the MAC state") is the only lever that
# makes a port always-forward regardless of STP -- and it is a whole-port
# switch, not per-VLAN/per-MSTI (mstp.config.msti.interface only exposes
# AdminPathCost/AdminPortPriority, no per-instance enable). Disabling it on
# Gi1/2 while Gi1/2 is still a member of every VLAN including VLAN 1 (its
# HybridVlans came back as literally all 4095 VLANs, native untagged VLAN 1)
# would open a real two-switch bridging loop for VLAN 1's flooded traffic --
# exactly the crash risk flagged earlier and in keti-reconfig's doc.
#
# The fix that keeps both guarantees: narrow Gi1/2 to being a member of ONLY
# the FRER VLAN (200) -- so VLAN 1 (and everything else) simply never uses
# Gi1/2 at all, on either switch, and can't loop through it no matter its
# STP state -- THEN disable STP on Gi1/2 only. Gi1/1 is untouched: still a
# member of every VLAN, still STP-protected, still the sole path for general
# traffic (so VLAN 1 keeps exactly the redundancy it had before: none on
# this link if Gi1/1 itself fails, unchanged from a single-link segment).
# VLAN 200 itself can't form a loop irrespective of STP: Generation only
# exists on switch2's ingress side and Recovery only egresses toward the PC
# on switch1 -- nothing ever reinjects a frame back into the ring -- so
# taking VLAN 200 out of STP's hands is safe by construction, not by luck.
#
# Net effect: the classified ESP32-1->PC stream gets true simultaneous
# dual-path replication (real zero-loss FRER), while unrelated traffic
# keeps using Gi1/1 alone, same as it already did with Gi1/2 sitting
# AlternatePort/discarding for it. Storm policers on both ports first, as
# a hard backstop in case this reasoning has a hole -- cheap and reversible.
STORM_PORTS = ["Gi 1/1", "Gi 1/2"]


def arm_storm_policer(url, port):
    for kind in ("broadcast", "unicast", "unknown"):
        try:
            rpc(url, f"qos.config.interface.stormPolicer.{kind}.set",
                [port, {"Enable": True, "FrameRate": True, "Cir": 1000}])
        except RuntimeError as e:
            print(f"  (storm policer {kind} on {port} @ {url}: {e})")


def narrow_gi12_to_frer_vlan_only(url, port, vlan):
    cfg = rpc(url, "vlan.config.interface.get", [port])
    cfg["HybridVlans"] = [vlan]
    cfg["HybridNativeVlan"] = vlan
    rpc(url, "vlan.config.interface.set", [port, cfg])


def disable_stp(url, port):
    cfg = rpc(url, "mstp.config.cist.interface.get", [port])
    if cfg["Enable"]:
        cfg["Enable"] = False
        rpc(url, "mstp.config.cist.interface.set", [port, cfg])


print("--- arming storm policers on Gi1/1+Gi1/2 (both switches) before touching STP ---")
for sw in (SWITCH1, SWITCH2):
    for p in STORM_PORTS:
        arm_storm_policer(sw, p)

print("--- narrowing Gi1/2 to VLAN 200 only (both switches) ---")
for sw in (SWITCH1, SWITCH2):
    narrow_gi12_to_frer_vlan_only(sw, "Gi 1/2", FRER_VLAN)

print("--- disabling STP on Gi1/2 only (both switches) -- Gi1/1 untouched ---")
for sw in (SWITCH1, SWITCH2):
    disable_stp(sw, "Gi 1/2")

print("\nBoth sides configured. Gi1/2 now always-forwards for VLAN 200 only;")
print("Gi1/1 keeps carrying everything else under normal STP, unchanged.")

# --------------------------------------------------------------------------
# Direction B: PC -> ESP32-1. Same VLAN/STP infrastructure as direction A
# above (Gi1/1 + Gi1/2 ring, VLAN 200, flooding off, STP off on Gi1/2 only)
# -- that part is direction-agnostic, already done. This just mirrors the
# Generation/Recovery roles: switch1 (PC's side) becomes Generation now,
# switch2 (ESP32-1's side) becomes Recovery. A second, independent FRER
# instance and VCL stream (inst/stream 2, not 1) since a switch can't reuse
# the same instance for two different Mode/EgressPorts configs, and
# InstanceMax is 127 per frer.capabilities.get -- plenty of room.
#
# Deliberately proven with PC-originated ping/traffic rather than waiting
# on ESP32-1's own zenoh session: Recovery's own Passed counter increments
# the moment it deduplicates and forwards a real R-tagged frame toward
# Gi1/5, regardless of whether the board at the far end of that last hop
# can actually receive it -- so this direction is a real, independent way
# to prove the FRER mechanism itself works, even while ESP32-1's physical
# link issue is still unresolved.
STREAM_ID_B = 2
FRER_INST_B = 2

print(f"\n--- direction B: stream {STREAM_ID_B}, src {PC_MAC} -> dst {ESP32_1_MAC} (PC -> ESP32-1) ---")

print(f"--- switch1 (Generation): ingress Gi 1/6 (from the PC) ---")
create_stream(SWITCH1, STREAM_ID_B, stream_match_conf(ESP32_1_MAC, PC_MAC))
rpc(SWITCH1, "vcl.config.interface.stream.add", ["Gi 1/6", STREAM_ID_B])
create_frer(SWITCH1, FRER_INST_B, frer_conf("generation", STREAM_ID_B, RING_PORTS))
gen_status_b = rpc(SWITCH1, "frer.status.get", [FRER_INST_B])
print("frer.status (switch1/Generation, dir B):", gen_status_b)

print(f"--- switch2 (Recovery): ingress {RING_PORTS}, egress toward ESP32-1 ({ESP32_1_INGRESS_ON_SWITCH2}) ---")
create_stream(SWITCH2, STREAM_ID_B, stream_match_conf(ESP32_1_MAC, PC_MAC))
for p in RING_PORTS:
    rpc(SWITCH2, "vcl.config.interface.stream.add", [p, STREAM_ID_B])
create_frer(SWITCH2, FRER_INST_B, frer_conf("recovery", STREAM_ID_B, [ESP32_1_INGRESS_ON_SWITCH2]))
rec_status_b = rpc(SWITCH2, "frer.status.get", [FRER_INST_B])
print("frer.status (switch2/Recovery, dir B):", rec_status_b)

print("\nBoth directions configured:")
print(f"  A: ESP32-1 -> PC   (stream {STREAM_ID}, inst {FRER_INST}: switch2 Generation, switch1 Recovery)")
print(f"  B: PC -> ESP32-1   (stream {STREAM_ID_B}, inst {FRER_INST_B}: switch1 Generation, switch2 Recovery)")

# --------------------------------------------------------------------------
# The actual replication mechanism, found the hard way: this switch has no
# dedicated "duplicate to N ports" FRER hardware path. It replicates by
# FLOODING the R-tagged frame within the FRER VLAN -- since nothing else
# ever lives in VLAN 200, a Generation frame's destination is always
# "unknown" there, and normal flood-to-all-members behaviour *is* the
# duplication. Proved directly: with vlan.config.global.flooding temporarily
# re-enabled for VLAN 200, Recovery's Passed counter went non-zero for the
# first time all day.
#
# But that's also a real, live loop: Gi1/2 has no STP protection (needed so
# it always-forwards for FRER), so an unknown-destination frame in VLAN 200
# bounces switch1<->switch2 over Gi1/1+Gi1/2 forever. Watched it happen:
# both switches' leaf-port discard counters climbed by tens of millions per
# second within seconds of enabling flooding, and it knocked out ESP32-2's
# real, unrelated session. Reverted (flooding back off) before it did any
# more damage. MSTI-per-VLAN mapping doesn't fix this either: STP will
# always block one of two parallel links between the same two bridges in
# ANY instance -- that's the whole point of spanning tree -- so it can't
# give "both links forwarding" no matter which MSTI VLAN 200 sits in.
#
# The actual fix: replicate WITHOUT flooding. mac.config.fdb.static.add's
# PortList ("List of destination ports for which frames with this DMAC is
# forwarded to") accepts more than one port per entry -- a static multicast-
# style FDB entry. Adding one for each direction's real destination MAC,
# scoped to VLAN 200 only (dynamic learning in VLAN 1 for the same MACs is
# untouched -- VLAN is part of the FDB key), makes Generation duplicate
# deterministically onto both ring ports with NO flooding involved anywhere,
# so there's nothing left that can bounce. Confirmed: Gi1/2's real packet
# counters climbed in step with a ping burst, TxDiscardPkts stayed at 0
# throughout, and Recovery's Passed counter kept climbing right along with
# it -- direction B proven end-to-end, safely, with flooding OFF the entire
# time.
print("\n--- static FDB duplication (the real, loop-free replication mechanism) ---")
rpc(SWITCH2, "mac.config.fdb.static.add", [FRER_VLAN, PC_MAC, {"PortList": RING_PORTS}])
rpc(SWITCH1, "mac.config.fdb.static.add", [FRER_VLAN, ESP32_1_MAC, {"PortList": RING_PORTS}])
print(f"  switch2: VLAN {FRER_VLAN} dst {PC_MAC} -> {RING_PORTS} (direction A duplication)")
print(f"  switch1: VLAN {FRER_VLAN} dst {ESP32_1_MAC} -> {RING_PORTS} (direction B duplication)")
for sw in (SWITCH1, SWITCH2):
    flooding = rpc(sw, "vlan.config.global.flooding.get", [FRER_VLAN])
    assert flooding is False, f"VLAN {FRER_VLAN} flooding must stay OFF on {sw} -- it's the loop risk, not the fix"
print(f"  confirmed: VLAN {FRER_VLAN} flooding is OFF on both switches (must stay that way)")

print("\nGoal: frer.statistics.get on each Recovery side shows Passed > 0, with zero")
print("TxDiscardPkts growth anywhere -- both true now for direction B (verified live with a")
print("ping burst to 192.168.100.60). Direction A is configured identically and will behave")
print("the same the moment ESP32-1's own hardware/cable issue is resolved.")

# --------------------------------------------------------------------------
# Considered, and deliberately NOT done: extending the static-FDB
# duplication trick from VLAN 200 to VLAN 1 (general traffic) and disabling
# STP on Gi1/1 too, at the user's request for "전부 이중화 양방향" (duplicate
# everything, both directions) / "다중화도 스피닝트리 끄고" (STP off there
# too). Started implementing it, then caught a real problem before running
# anything against the live switches: VLAN 1 has flooding ENABLED
# (`vlan.config.global.flooding.get(1)` -> true, needed for ARP to work at
# all on this segment). The instant Gi1/2 becomes a VLAN 1 member while its
# STP is already off, ANY ARP broadcast or unknown-destination frame
# reopens the exact same switch1<->switch2 bounce loop as the earlier VLAN
# 200 incident -- except on the segment's main VLAN, not a purpose-built
# empty one, so the blast radius is much worse. Static FDB entries only
# help known unicast MACs; they don't touch broadcast/ARP, which always
# floods. Disabling VLAN 1 flooding too would "fix" the loop risk but
# breaks ARP for anything not statically covered -- too disruptive for
# this segment's main VLAN. So general traffic stays on Gi1/1 under normal
# STP (already verified safe, 0.51s worst-case failover); only the two
# FRER-classified streams (their own dedicated, flooding-disabled VLAN
# 200) get the STP-off/static-duplication treatment above, where it's
# actually safe.
