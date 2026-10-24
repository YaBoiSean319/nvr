import type { Outcome } from "./tabDraft";

/** Tab-scoped outcome plus Revert and Save, disabled when there is nothing to do. */
export function TabFooter({ outcome, dirty, saving, onRevert, blocked }: {
  outcome: Outcome | null; dirty: boolean; saving: boolean; onRevert: () => void; blocked?: string | null;
}) {
  return (
    <>
      {blocked && dirty ? <p className="field-error" role="alert">{blocked}</p> : null}
      {outcome ? <p className="notice" data-tone={outcome.tone} role={outcome.tone === "good" ? "status" : "alert"}>{outcome.text}</p> : null}
      <footer className="tab-actions">
        <button type="button" className="button" onClick={onRevert} disabled={!dirty || saving}>Revert</button>
        <button type="submit" className="button" data-variant="primary" disabled={!dirty || saving || Boolean(blocked)}>{saving ? "Saving…" : "Save"}</button>
      </footer>
    </>
  );
}
