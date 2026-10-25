import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { useDisplaySettings } from "@/api/queries";
import type { Camera } from "@/api/types";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { useUnmaskedStore } from "@/features/privacy/store";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import type { Zone } from "./api";
import { ZoneEditor } from "./ZoneEditor";

const MAX_AREAS = 8;

interface Privacy {
  zones: Zone[];
  camera_side: "none" | "applied" | "unsupported" | "failed";
  camera_masked_since: string | null;
  recordings: string;
  server_enforced: boolean;
  unmasked_until: string | null;
}

const CAMERA_SIDE: Record<Privacy["camera_side"], string> = {
  none: "No masks are set on the camera.",
  applied: "The camera applies these masks itself.",
  unsupported: "This camera does not accept privacy masks; the NVR masks the picture itself.",
  failed: "The masks could not be written to the camera; the NVR masks the picture itself until they are.",
};

const at = (cameraId: string, path = "") => `/api/cameras/${encodeURIComponent(cameraId)}/privacy${path}`;

/** Areas nobody sees, live or recorded, and an administrator's audited way to see past them. */
export function PrivacyTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const display = useDisplaySettings();
  const queryKey = ["cameras", camera.id, "privacy"] as const;
  const privacy = useQuery({ queryKey, queryFn: ({ signal }) => api<Privacy>(at(camera.id), { signal }) });
  const current = useMemo(() => (privacy.data ? { zones: privacy.data.zones } : undefined), [privacy.data]);
  const unmaskedUntil = useUnmaskedStore(store => store.until[camera.id]);
  const { grant, clear } = useUnmaskedStore.getState();
  const [reason, setReason] = useState("");
  const [grantError, setGrantError] = useState<string | null>(null);
  const tab = useTabDraft(current, {
    label: `Privacy masks for ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const saved = await api<Privacy>(at(camera.id), { method: "PUT", body: draft });
        client.setQueryData(queryKey, { ...saved, unmasked_until: privacy.data?.unmasked_until ?? null });
        return { ok: true, base: { zones: saved.zones }, outcome: { tone: "good", text: `Saved. ${CAMERA_SIDE[saved.camera_side]}` } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Masks not saved: ${failureText(error)}.` } };
      }
    },
  });
  if (privacy.isPending) return <p className="muted">Loading privacy masks…</p>;
  if (privacy.error || !tab.draft) return <p className="notice" data-tone="bad" role="alert">{failureText(privacy.error, "Privacy masks could not be loaded.")}</p>;
  const data = privacy.data!;
  const when = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);
  const seeingUnmasked = (unmaskedUntil ?? 0) > Date.now();

  return (
    <div className="tab-form privacy-tab">
      <form aria-label="Privacy masks" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
        <p className="notice" role="note" data-tone={data.zones.length && data.camera_side !== "applied" ? "warn" : undefined}>{data.recordings}</p>
        {data.zones.length ? <p className="muted field-hint">{CAMERA_SIDE[data.camera_side]}</p> : null}
        <fieldset className="field-group" disabled={!admin}>
          <legend>Masked areas</legend>
          <ZoneEditor cameraId={camera.id} zones={tab.draft.zones} onChange={zones => tab.change({ zones })} label="Masked area" readOnly={!admin}
                      max={MAX_AREAS} emptyHint="No masked areas: the whole picture is visible." />
          <p className="muted field-hint">Masked areas are black in Live, Playback, snapshots, alarm pictures, notifications and exports. Where the camera accepts ONVIF privacy masks they are written to it too, so its recordings carry them.</p>
        </fieldset>
        <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} />
      </form>

      {admin && data.zones.length ? (
        <section className="field-group" aria-label="Unmasked viewing">
          <h3 className="field-heading">See unmasked footage</h3>
          {seeingUnmasked ? (
            <div className="field-actions" role="status">
              <span>You see this camera unmasked in Live and Playback until {when(new Date(unmaskedUntil!).toISOString())}.</span>
              <button type="button" className="button" onClick={() => {
                clear(camera.id);
                void api(at(camera.id, "/unmasked"), { method: "DELETE" }).catch(error => setGrantError(failureText(error)));
              }}>Mask again</button>
            </div>
          ) : (
            <form className="field-group plain" aria-label="Reason for unmasked viewing" onSubmit={event => {
              event.preventDefault();
              setGrantError(null);
              void api<{ expires_at: string }>(at(camera.id, "/unmasked"), { method: "POST", body: { reason } })
                .then(result => { grant(camera.id, result.expires_at); setReason(""); })
                .catch(error => setGrantError(failureText(error)));
            }}>
              <p className="muted field-hint">For 15 minutes, only you see this camera unmasked in Live, Playback and snapshots. Exports stay masked. Your reason and every unmasked view are recorded in the audit log.</p>
              <label className="field"><span>Reason</span>
                <textarea value={reason} rows={2} maxLength={500} onChange={event => setReason(event.target.value)} />
              </label>
              {grantError ? <p className="field-error" role="alert">{grantError}</p> : null}
              <div className="field-actions"><button type="submit" className="button" disabled={reason.trim().length < 10}>View unmasked for 15 minutes</button></div>
            </form>
          )}
        </section>
      ) : null}
    </div>
  );
}
