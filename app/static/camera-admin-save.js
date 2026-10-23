(function installCameraAdminSave(root, createModule) {
  const save = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = save;
  root.NvrCameraAdminSave = save;
})(typeof window === "undefined" ? globalThis : window, function createCameraAdminSave() {
  async function runScopedSave(operations, { lock, onStart, onFinish } = {}) {
    const ordered = [...new Set(operations || [])].filter(Boolean);
    const result = { savedLabels: [], savedOperations: [], failedOperation: null, error: null, complete: true };
    if (!ordered.length) return result;

    let unlock = () => {};
    try {
      unlock = lock?.(ordered) || unlock;
      onStart?.(ordered);
      for (const operation of ordered) {
        result.failedOperation = operation;
        if (typeof operation.save !== "function") {
          throw new Error(operation.unavailableMessage || "Saving this configuration is not available.");
        }
        await operation.save();
        if (operation.isCurrent && !operation.isCurrent()) {
          throw new Error(operation.staleMessage || "The selected configuration changed while saving.");
        }
        operation.clear?.(operation.scope, operation);
        result.savedOperations.push(operation);
        const labels = operation.labels || [operation.label];
        result.savedLabels.push(...labels.filter(Boolean));
      }
    } catch (error) {
      result.error = error instanceof Error ? error : new Error(String(error));
      result.complete = false;
    } finally {
      try {
        unlock();
      } finally {
        onFinish?.(result);
      }
    }
    if (result.complete) result.failedOperation = null;
    return result;
  }

  return { runScopedSave };
});
