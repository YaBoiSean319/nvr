import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import { useDraft } from "./drafts";

export interface Outcome {
  tone: "good" | "warn" | "bad";
  text: string;
}

export interface SaveResult<T> {
  /** Published values after the save, including any portion that did save. */
  base: T;
  outcome: Outcome;
  ok: boolean;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export const failureText = (error: unknown, fallback = "the NVR could not be reached") => (error instanceof ApiError ? error.detail : fallback);

/**
 * One editor tab's draft: explicit Save and Revert, refreshes that never overwrite edits,
 * partial saves that keep only the unsaved part dirty, and the shared leave guard.
 */
export function useTabDraft<T>(published: T | undefined, options: {
  label: string;
  editable: boolean;
  save: (draft: T, base: T) => Promise<SaveResult<T>>;
}) {
  const [base, setBase] = useState<T | undefined>(published);
  const [draft, setDraft] = useState<T | undefined>(published);
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const alive = useRef(true);
  const dirty = base !== undefined && draft !== undefined && !same(base, draft);

  useEffect(() => () => {
    alive.current = false;
  }, []);

  useEffect(() => {
    if (published === undefined) return;
    setBase(previous => (previous !== undefined && same(previous, published) ? previous : published));
    setDraft(current => {
      if (current === undefined) return published;
      // Only an untouched draft follows the refresh; edits in progress stay as they are.
      return base !== undefined && same(current, base) && !same(current, published) ? published : current;
    });
  }, [published]); // eslint-disable-line react-hooks/exhaustive-deps

  async function save(): Promise<boolean> {
    if (!dirty || saving || draft === undefined || base === undefined) return !dirty;
    setSaving(true);
    const result = await options.save(draft, base);
    if (!alive.current) return result.ok;
    setSaving(false);
    setBase(result.base);
    if (result.ok) setDraft(result.base);
    setOutcome(result.outcome);
    return result.ok;
  }

  function revert() {
    setDraft(base);
    setOutcome(null);
  }

  function change(update: Partial<T> | ((current: T) => T)) {
    setDraft(current => (current === undefined ? current : typeof update === "function" ? update(current) : { ...current, ...update }));
    setOutcome(null);
  }

  useDraft(dirty && options.editable, { label: options.label, save, discard: revert });
  return { base, draft, dirty, saving, outcome, setOutcome, save, revert, change };
}
