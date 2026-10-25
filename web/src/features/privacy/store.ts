import { create } from "zustand";

/**
 * Cameras this administrator chose to see unmasked, until their grant lapses. Kept in
 * memory only: a reload or sign-out shows every camera masked again. The server decides;
 * asking without a grant is refused.
 */
interface UnmaskedState {
  until: Record<string, number>;
  grant: (cameraId: string, expiresAt: string) => void;
  clear: (cameraId: string) => void;
  reset: () => void;
}

export const useUnmaskedStore = create<UnmaskedState>(set => ({
  until: {},
  grant: (cameraId, expiresAt) => set(state => ({ until: { ...state.until, [cameraId]: Date.parse(expiresAt) } })),
  clear: cameraId => set(state => {
    const until = { ...state.until };
    delete until[cameraId];
    return { until };
  }),
  reset: () => set({ until: {} }),
}));

export function isUnmasked(cameraId: string): boolean {
  return (useUnmaskedStore.getState().until[cameraId] ?? 0) > Date.now();
}

/** A media address, asking for the unmasked picture when this person holds a grant for the camera. */
export function withUnmasked(url: string, cameraId: string): string {
  return isUnmasked(cameraId) ? `${url}${url.includes("?") ? "&" : "?"}unmasked=true` : url;
}
