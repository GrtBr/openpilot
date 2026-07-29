// GRT-MOD — fork-owned param keys, #included INSIDE the `keys` initializer in params_keys.h.
// Kept in a separate file so params_keys.h carries exactly one fork line (one merge conflict
// point instead of one per key). Same entry syntax as params_keys.h.
//
// MapdSettings MUST be PERSISTENT: Params::clear_all() unlinks every file in the params dir
// whose name is not in this table, and manager.py calls it on manager start and on every
// onroad/offroad/ignition transition. sunnypilot works around this by rewriting the file
// every second from mapd_manager.py; registering the key is the correct fix here.
{"MapdSettings", {PERSISTENT, JSON}},
{"SmartCruiseControlMap", {PERSISTENT, BOOL, "0"}},
// Firm hazard pre-braking (planner min() accel candidate). Separate from the master switch
// above and DEFAULT OFF: it is the most aggressive part of the feature and is meant to be
// enabled only after the speed-ceiling behaviour has been driven and validated.
{"SmartCruiseControlMapHazardAccel", {PERSISTENT, BOOL, "0"}},
// Set speed follows the posted limit (grt/set_speed.py, hook 3 in card.py). DEFAULT OFF and
// deliberately separate from SmartCruiseControlMap: this is the only fork feature that can
// RAISE the set speed, i.e. accelerate the car on OSM data.
{"SmartCruiseControlSetSpeed", {PERSISTENT, BOOL, "0"}},
