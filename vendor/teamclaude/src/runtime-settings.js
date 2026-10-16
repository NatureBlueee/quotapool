// Runtime settings that are safe to apply without restarting the data plane.
//
// The headless server already exposes POST /teamclaude/reload, but until now a
// changed switchThreshold was only read on process startup.  That made the
// operator choose between leaving every request parked and restarting the
// proxy (which drops those very requests).  Keep the small, mutable subset in
// one pure helper so reload behaviour is explicit and testable.

export function applyReloadableRuntimeSettings(
  diskConfig,
  liveConfig,
  accountManager,
) {
  const changed = {};
  const threshold = Number(diskConfig?.switchThreshold);
  if (Number.isFinite(threshold) && threshold > 0 && threshold <= 1) {
    if (liveConfig.switchThreshold !== threshold
        || accountManager.switchThreshold !== threshold) {
      changed.switchThreshold = threshold;
    }
    liveConfig.switchThreshold = threshold;
    accountManager.switchThreshold = threshold;
  }

  // connectionLimits() reads this object for every admission decision, so it
  // is safe to refresh in place as well.  This does not close existing
  // connections; it only changes the limits applied to future requests.
  if (diskConfig?.connectionManagement
      && typeof diskConfig.connectionManagement === 'object') {
    liveConfig.connectionManagement = {
      ...diskConfig.connectionManagement,
    };
    changed.connectionManagement = {
      ...liveConfig.connectionManagement,
    };
  }

  return changed;
}
