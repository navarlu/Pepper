# -*- coding: utf-8 -*-
# Python 2.x
# Capabilities snapshot for NAOqi / Choregraphe virtual robot (behaviors, emotions-ish, etc.)

import sys
import time
import qi

HOST = "10.0.0.149"
PORT = 9559  # change to 9560 if you launched NAOqi there

def get_service(session, name):
    try:
        return session.service(name)
    except Exception as e:
        return None

def call_safe(obj, method, *args, **kwargs):
    """Call obj.method(*args) if it exists; return (ok, result_or_error)."""
    try:
        fn = getattr(obj, method)
    except AttributeError:
        return (False, "missing")
    try:
        return (True, fn(*args, **kwargs))
    except Exception as e:
        return (False, str(e))

def main():
    # Connect
    url = "tcp://10.0.0.149:9559"
    print("=== Connecting to NAOqi at %s ===" % url)
    print("Connecting to %s ..." % url)
    s = qi.Session()
    try:
        s.connect(url)
    except Exception as e:
        print("ERROR: Cannot connect to NAOqi at %s\n%s" % (url, e))
        sys.exit(1)

    # Quick service inventory
    print("\n=== Service Inventory (first 60) ===")
    try:
        svcs = sorted(s.services())
    except:
        # Fallback for older qi
        svcs = []
    for name in svcs[:60]:
        print(" - %s" % name)
    if len(svcs) > 60:
        print("   ... (%d total)" % len(svcs))

    # Core proxies
    motion   = get_service(s, "ALMotion")
    posture  = get_service(s, "ALRobotPosture")
    leds     = get_service(s, "ALLeds")
    beh_mgr  = get_service(s, "ALBehaviorManager")
    life     = get_service(s, "ALAutonomousLife") or get_service(s, "AutonomousLife")
    dialog   = get_service(s, "ALDialog")
    mood     = get_service(s, "ALMood") or get_service(s, "ALRobotMood")
    anim_sp  = get_service(s, "ALAnimatedSpeech")
    mem      = get_service(s, "ALMemory")

    # Behaviors
    print("\n=== Behaviors ===")
    if beh_mgr:
        ok, installed = call_safe(beh_mgr, "getInstalledBehaviors")
        ok2, running  = call_safe(beh_mgr, "getRunningBehaviors")
        if ok:
            print("Installed behaviors (%d):" % len(installed))
            for b in sorted(installed):
                print("  * %s" % b)
        else:
            print("Installed behaviors: (unavailable: %s)" % installed)
        if ok2:
            print("Running behaviors (%d): %s" % (len(running), ", ".join(running) if running else "(none)"))
        else:
            print("Running behaviors: (unavailable: %s)" % running)
    else:
        print("ALBehaviorManager not available")

    # Postures
    print("\n=== Postures ===")
    if posture:
        ok, plist = call_safe(posture, "getPostureList")
        if ok:
            print("Available postures (%d): %s" % (len(plist), ", ".join(plist)))
        else:
            print("getPostureList unavailable: %s" % plist)
        # Current posture (if supported)
        ok, cur = call_safe(posture, "getPosture")
        if ok:
            print("Current posture: %s" % cur)
    else:
        print("ALRobotPosture not available")

    # LEDs
    print("\n=== LEDs ===")
    if leds:
        # group list name varies with versions; try both
        ok, groups = call_safe(leds, "getGroups")
        if not ok:
            ok, groups = call_safe(leds, "listGroups")
        if ok:
            print("LED groups (%d): %s" % (len(groups), ", ".join(groups)))
        else:
            print("LED group listing unavailable: %s" % groups)
    else:
        print("ALLeds not available")

    # Autonomous Life
    print("\n=== Autonomous Life ===")
    if life:
        ok, state = call_safe(life, "getState")
        if ok:
            print("Life state: %s (e.g., disabled/solitary/interactive)" % state)
        ok, focused = call_safe(life, "getFocus")
        if ok:
            print("Life focus: %s" % (focused if focused else "(none)"))
        ok, running = call_safe(life, "getActivity")
        if ok:
            print("Life activity: %s" % (running if running else "(none)"))
    else:
        print("ALAutonomousLife not available")

    # Dialog
    print("\n=== Dialog ===")
    if dialog:
        ok, langs = call_safe(dialog, "getSupportedLanguages")
        if ok:
            print("Supported languages: %s" % ", ".join(langs))
        ok, act = call_safe(dialog, "getActivatedTopics")
        if ok:
            print("Activated topics: %s" % (", ".join(act) if act else "(none)"))
        ok, all_topics = call_safe(dialog, "getAllLoadedTopics")
        if ok:
            print("Loaded topics: %s" % (", ".join(all_topics) if all_topics else "(none)"))
    else:
        print("ALDialog not available")

    # Mood / emotions (API varies across versions; we probe gently)
    print("\n=== Mood / Emotions (best-effort) ===")
    if mood:
        # Try a few known-ish calls across versions
        tried = False
        for meth in ["getEmotionState", "currentState", "getState", "getValence", "getEmotionValues"]:
            ok, res = call_safe(mood, meth)
            if ok:
                print("Mood.%s -> %s" % (meth, res))
                tried = True
        if not tried:
            print("Mood service present but no known getters available on this version.")
    else:
        print("ALMood/ALRobotMood not available")

    # Memory snapshot (a few popular keys if present)
    print("\n=== Memory keys (selected) ===")
    keys_of_interest = [
        "ALMemory/robot/state",
        "AutonomousLife/State",
        "ALTextToSpeech/Language",
        "ALBasicAwareness/EngagementMode",
        "ALMotion/RobotConfig",
        "Device/SubDeviceList/HeadYaw/Position/Sensor/Value"
    ]
    if mem:
        for k in keys_of_interest:
            ok, val = call_safe(mem, "getData", k)
            if ok:
                print(" - %s: %s" % (k, val))
    else:
        print("ALMemory not available")

    # Optional: small demo motion/LED sequence (safe for virtual robot)
    print("\n=== Quick demo (wake → StandInit → green face → rest) ===")
    motion = get_service(s, "ALMotion")
    posture = get_service(s, "ALRobotPosture")
    leds    = get_service(s, "ALLeds")
    if motion and posture and leds:
        try:
            motion.wakeUp()
            input("Press Enter to go to StandInit posture...")
            posture.goToPosture("StandInit", 0.5)
            leds.fadeRGB("FaceLeds", 0x00FF00, 0.5)  # green
            time.sleep(1.0)
            motion.rest()
            print("Demo completed.")
        except Exception as e:
            print("Demo skipped: %s" % e)
    else:
        print("Demo skipped: missing one of ALMotion/ALRobotPosture/ALLeds")

    print("\nDone.")

if __name__ == "__main__":
    main()
