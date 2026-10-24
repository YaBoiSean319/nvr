import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import { keys } from "@/api/queries";
import type { Branding } from "@/api/types";
import { Modal } from "@/components/Modal";
import { TabFooter } from "@/features/admin/TabFooter";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";

type AssetKey = "primary" | "light" | "dark" | "icon";
const ASSETS: { key: AssetKey; label: string; hint: string }[] = [
  { key: "primary", label: "Primary logo", hint: "Shown in the header when no theme-specific logo applies." },
  { key: "light", label: "Light-theme logo", hint: "Used on light backgrounds." },
  { key: "dark", label: "Dark-theme logo", hint: "Used on dark backgrounds." },
  { key: "icon", label: "Icon", hint: "Browser tab and app icon." },
];
const TYPES = ["image/png", "image/webp", "image/jpeg"];
const MAX_BYTES = 2 * 1024 * 1024;

type AssetChange = { action: "keep" } | { action: "remove" } | { action: "upload"; media_type: string; data_base64: string; preview: string; file: string };

interface Draft {
  app_name: string;
  primary_color: string;
  accent_color: string;
  assets: Record<AssetKey, AssetChange>;
}

const KEEP: Record<AssetKey, AssetChange> = { primary: { action: "keep" }, light: { action: "keep" }, dark: { action: "keep" }, icon: { action: "keep" } };

function fromBranding(branding: Branding): Draft {
  return { app_name: branding.app_name, primary_color: branding.primary_color, accent_color: branding.accent_color, assets: KEEP };
}

async function readImage(file: File): Promise<{ media_type: string; data_base64: string; preview: string } | string> {
  if (!TYPES.includes(file.type)) return "Choose a PNG, WebP or JPEG image.";
  if (file.size > MAX_BYTES) return "Each image must be no larger than 2 MiB.";
  const preview = URL.createObjectURL(file);
  try {
    await createImageBitmap(file);
  } catch {
    URL.revokeObjectURL(preview);
    return "This file is not a readable image.";
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  return { media_type: file.type, data_base64: btoa(binary), preview };
}

/**
 * Site name, colours and logos. The preview here is local to this page: nobody, including you,
 * sees a change until it is saved, and a failed save leaves the published branding as it was.
 */
export function AppearanceSettings() {
  const client = useQueryClient();
  const branding = useQuery({ queryKey: ["settings", "branding"], queryFn: ({ signal }) => api<Branding>("/api/settings/branding", { signal }) });
  const published = useMemo(() => (branding.data ? fromBranding(branding.data) : undefined), [branding.data]);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<AssetKey, string>>>({});
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);

  function publish(saved: Branding) {
    client.setQueryData(["settings", "branding"], saved);
    void client.invalidateQueries({ queryKey: keys.branding });
  }

  const tab = useTabDraft(published, {
    label: "Appearance and branding",
    editable: true,
    save: async (draft, base) => {
      setFieldErrors({});
      const assets = Object.fromEntries(Object.entries(draft.assets)
        .filter(([, change]) => change.action !== "keep")
        .map(([key, change]) => [key, change.action === "remove" ? null : { media_type: (change as { media_type: string }).media_type, data_base64: (change as { data_base64: string }).data_base64 }]));
      try {
        const saved = await api<Branding>("/api/settings/branding", { method: "PUT", body: { app_name: draft.app_name, primary_color: draft.primary_color, accent_color: draft.accent_color, assets } });
        publish(saved);
        return { ok: true, base: fromBranding(saved), outcome: { tone: "good", text: "Saved. Everyone sees the new branding." } };
      } catch (error) {
        // Server errors name the asset ("Icon: …"); show them on that field.
        const detail = error instanceof ApiError ? error.detail : "";
        const asset = ASSETS.find(item => detail.startsWith(`${item.label}: `));
        if (asset) setFieldErrors({ [asset.key]: detail.slice(asset.label.length + 2) });
        return { ok: false, base, outcome: { tone: "bad", text: `Not saved; the published branding is unchanged. ${failureText(error)}.` } };
      }
    },
  });

  async function choose(key: AssetKey, file: File | undefined) {
    if (!file) return;
    const result = await readImage(file);
    if (typeof result === "string") {
      setFieldErrors(current => ({ ...current, [key]: result }));
      return;
    }
    setFieldErrors(current => ({ ...current, [key]: undefined }));
    tab.change(current => ({ ...current, assets: { ...current.assets, [key]: { action: "upload", ...result, file: file.name } } }));
  }

  async function resetDefaults() {
    setResetError(null);
    try {
      const saved = await api<Branding>("/api/settings/branding", { method: "PUT", body: { app_name: "Workplace NVR", primary_color: "#17212b", accent_color: "#69a1ff", reset_defaults: true } });
      publish(saved);
      tab.revert();
      setResetting(false);
    } catch (error) {
      setResetError(failureText(error));
    }
  }

  if (!tab.draft || !branding.data) return <p className="muted">{branding.error ? failureText(branding.error) : "Loading…"}</p>;
  const draft = tab.draft;
  const colorProblem = [draft.primary_color, draft.accent_color].some(color => !/^#[0-9a-fA-F]{6}$/.test(color)) ? "Use six-digit hex colours such as #1a2b3c." : null;
  const assetUrl = (key: AssetKey) => {
    const change = draft.assets[key];
    if (change.action === "upload") return change.preview;
    if (change.action === "remove") return null;
    return branding.data.assets[key]?.url ?? null;
  };
  const logo = assetUrl("primary") ?? assetUrl("light");

  return (
    <form className="tab-form tab-form-wide" aria-label="Appearance and branding" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <div className="brand-preview" aria-label="Branding preview (not saved)" role="img" style={{ borderTopColor: draft.primary_color }}>
        <span className="brand-preview-mark" style={{ background: draft.primary_color }}>{logo ? <img src={logo} alt="" /> : draft.app_name.slice(0, 1)}</span>
        <span className="brand-preview-name">{draft.app_name || "Untitled"}</span>
        <span className="brand-preview-button" style={{ background: draft.accent_color }}>Button</span>
        <span className="muted">Preview only</span>
      </div>
      <fieldset className="field-group">
        <legend>Name and colours</legend>
        <label className="field"><span>Application name</span><input value={draft.app_name} maxLength={80} onChange={event => tab.change({ app_name: event.target.value })} /></label>
        <div className="field-row">
          <label className="field"><span>Primary colour</span>
            <span className="color-field"><input type="color" value={draft.primary_color} aria-label="Primary colour picker" onChange={event => tab.change({ primary_color: event.target.value })} />
              <input className="mono" value={draft.primary_color} aria-label="Primary colour hex" onChange={event => tab.change({ primary_color: event.target.value })} /></span>
          </label>
          <label className="field"><span>Accent colour</span>
            <span className="color-field"><input type="color" value={draft.accent_color} aria-label="Accent colour picker" onChange={event => tab.change({ accent_color: event.target.value })} />
              <input className="mono" value={draft.accent_color} aria-label="Accent colour hex" onChange={event => tab.change({ accent_color: event.target.value })} /></span>
          </label>
        </div>
        <p className="muted field-hint">The accent is adjusted automatically where needed so text on it stays readable in light and dark themes.</p>
      </fieldset>
      <fieldset className="field-group">
        <legend>Logos and icon</legend>
        <div className="asset-grid">
          {ASSETS.map(asset => {
            const url = assetUrl(asset.key);
            const change = draft.assets[asset.key];
            return (
              <div key={asset.key} className="asset-field" role="group" aria-label={asset.label}>
                <strong>{asset.label}</strong>
                <span className="asset-thumb">{url ? <img src={url} alt={`${asset.label} preview`} /> : <span className="muted">None</span>}</span>
                <span className="muted field-hint">{change.action === "upload" ? `New: ${change.file}` : change.action === "remove" ? "Will be removed" : asset.hint}</span>
                <span className="field-actions">
                  <label className="button file-button">Upload<input type="file" accept={TYPES.join(",")} aria-label={`Upload ${asset.label}`} onChange={event => void choose(asset.key, event.target.files?.[0])} /></label>
                  {branding.data.assets[asset.key] && change.action !== "remove" ? (
                    <button type="button" className="button" data-variant="ghost" onClick={() => tab.change(current => ({ ...current, assets: { ...current.assets, [asset.key]: { action: "remove" } } }))}>Remove</button>
                  ) : null}
                  {change.action !== "keep" ? (
                    <button type="button" className="button" data-variant="ghost" onClick={() => tab.change(current => ({ ...current, assets: { ...current.assets, [asset.key]: { action: "keep" } } }))}>Undo</button>
                  ) : null}
                </span>
                {fieldErrors[asset.key] ? <span className="field-error" role="alert">{fieldErrors[asset.key]}</span> : null}
              </div>
            );
          })}
        </div>
      </fieldset>
      <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={() => { setFieldErrors({}); tab.revert(); }} blocked={colorProblem ?? (draft.app_name.trim() ? null : "The application needs a name.")} />
      <div className="field-actions">
        <button type="button" className="button" data-variant="danger" onClick={() => setResetting(true)}>Reset to defaults…</button>
      </div>
      <Modal open={resetting} onOpenChange={setResetting} title="Reset branding to defaults?"
             description="The name, colours and every uploaded logo return to the defaults for everyone."
             footer={<><button type="button" className="button" onClick={() => setResetting(false)}>Cancel</button><button type="button" className="button" data-variant="danger" onClick={() => void resetDefaults()}>Reset to defaults</button></>}>
        {resetError ? <p className="notice" data-tone="bad" role="alert">{resetError}</p> : null}
      </Modal>
    </form>
  );
}
