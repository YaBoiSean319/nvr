import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useCameraGroups, useCameras, useDisplaySettings } from "@/api/queries";
import { api } from "@/api/client";
import { Modal } from "@/components/Modal";
import { formatSiteDateTime } from "@/lib/zonedTime";
import type { ScheduleInterval } from "../cameras/api";
import { DIRECTION_LABEL, LineEditor, type CrossingDirection } from "../cameras/LineEditor";
import { ScheduleEditor, scheduleProblem } from "../cameras/ScheduleEditor";
import { ZoneEditor } from "../cameras/ZoneEditor";
import { guarded } from "../drafts";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import { ActionsEditor, DeliveryLog } from "./ActionsEditor";
import {
  CONDITION_LABEL, TAMPER_KINDS, TAMPER_LABEL, TRAVEL_LABEL, createRule, definitionOf, definitionProblem, deleteRule, listRules, newRule, ruleKeys, switchCondition, testRule, updateRule,
  type Condition, type ObjectClass, type Rule, type RuleDefinition, type TamperKind, type TestResult, type TravelDirection,
} from "./api";

const NEW = "new";
const splitList = (text: string) => text.split(",").map(item => item.trim()).filter(Boolean);
const CLASS_LABEL: Record<ObjectClass, string> = { person: "People", vehicle: "Vehicles" };

function summary(rule: Rule): string {
  const subject = rule.condition.type === "tamper"
    ? rule.condition.kinds.map(kind => TAMPER_LABEL[kind].toLowerCase()).join(", ")
    : rule.condition.type === "external_event"
    ? (rule.condition.types.join(", ") || "any event")
    : rule.classes.map(item => CLASS_LABEL[item].toLowerCase()).join(" and ");
  return `${CONDITION_LABEL[rule.condition.type]} · ${subject}${rule.enabled ? "" : " · disabled"}`;
}

/** Rules beside one rule editor; phones drill into the editor and come back. */
export function RulesWorkspace() {
  const rules = useQuery({ queryKey: ruleKeys.all, queryFn: ({ signal }) => listRules(signal) });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // A new editor for each choice in the list; saving a new rule keeps its editor (and outcome) in place.
  const [opened, setOpened] = useState(0);
  const selected = selectedId === NEW ? null : (rules.data ?? []).find(rule => rule.id === selectedId) ?? null;
  const open = (id: string | null) => void guarded(() => { setSelectedId(id); setOpened(count => count + 1); });

  return (
    <div className="admin-panes groups-panes" data-detail={selectedId ? "open" : "closed"}>
      <section className="inventory" aria-label="Rules">
        <div className="inventory-tools">
          <button type="button" className="button" data-variant="primary" onClick={() => open(NEW)}>New rule</button>
        </div>
        <p className="inventory-meta">{rules.data?.length ?? 0} {rules.data?.length === 1 ? "rule" : "rules"}</p>
        <div className="inventory-scroll">
          {rules.isPending ? <p className="muted inventory-empty">Loading rules…</p> : null}
          {rules.error ? <p className="notice inventory-empty" data-tone="bad" role="alert">{failureText(rules.error, "Rules could not be loaded.")}</p> : null}
          {rules.data && !rules.data.length ? (
            <p className="muted inventory-empty">No rules yet. A rule watches cameras for people or vehicles entering a zone, crossing a line, staying too long or moving the wrong way.</p>
          ) : null}
          <ul className="group-list">
            {(rules.data ?? []).map(rule => (
              <li key={rule.id}>
                <button type="button" className="row-select group-row" aria-current={rule.id === selectedId ? "true" : undefined}
                        onClick={() => open(rule.id)}>
                  <span>{rule.name}</span>
                  <span className="muted">{summary(rule)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </section>
      <div className="pane-resizer" aria-hidden="true" />
      <div className="editor-pane">
        {selectedId === NEW || selected ? (
          <RuleEditor key={opened} rule={selected} onSaved={rule => setSelectedId(rule.id)} onClose={() => open(null)}
                      onDeleted={() => setSelectedId(null)} />
        ) : selectedId && rules.data ? (
          <div className="editor-empty" role="status">
            <p><strong>This rule no longer exists.</strong></p>
            <button type="button" className="button" onClick={() => setSelectedId(null)}>Close</button>
          </div>
        ) : <div className="editor-empty"><p><strong>Select a rule, or create one.</strong></p></div>}
      </div>
    </div>
  );
}

function RuleEditor({ rule, onSaved, onClose, onDeleted }: {
  rule: Rule | null; onSaved: (rule: Rule) => void; onClose: () => void; onDeleted: () => void;
}) {
  const client = useQueryClient();
  const cameras = useCameras();
  const groups = useCameraGroups();
  const display = useDisplaySettings();
  const eventSources = useQuery({ queryKey: ["admin", "event-sources"], queryFn: ({ signal }) => api<{ sources: { id: string; name: string }[] }>("/api/event-sources", { signal }).then(result => result.sources) });
  const published = useMemo(() => (rule ? definitionOf(rule) : newRule()), [rule]);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<{ result: TestResult } | { error: string } | null>(null);
  const title = rule ? rule.name : "New rule";
  const tab = useTabDraft<RuleDefinition>(published, {
    label: rule ? `Rule ${rule.name}` : "the new rule",
    editable: true,
    save: async (draft, base) => {
      try {
        const saved = rule ? await updateRule(rule.id, draft) : await createRule(draft);
        await client.invalidateQueries({ queryKey: ruleKeys.all });
        if (!rule) onSaved(saved);
        return { ok: true, base: definitionOf(saved), outcome: { tone: "good", text: saved.enabled ? "Saved. The rule applies to what cameras see from now on." : "Saved. The rule is disabled." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Rule not saved: ${failureText(error)}.` } };
      }
    },
  });
  const draft = tab.draft!;
  // A test result describes the rule as it was tested; any change makes it out of date.
  const draftKey = JSON.stringify(draft);
  useEffect(() => setTest(null), [draftKey]);
  const live = (cameras.data ?? []).filter(camera => !camera.archived_at);
  // Zones and lines are drawn over a frame from the first camera the rule watches.
  const previewCamera = draft.camera_ids[0]
    ?? (groups.data ?? []).find(group => draft.group_ids.includes(group.id))?.camera_ids[0]
    ?? null;
  const intervals: ScheduleInterval[] = (draft.schedule?.intervals ?? []).map(interval => ({ ...interval, mode: "continuous" }));
  const problem = definitionProblem(draft) ?? (draft.schedule ? scheduleProblem(intervals) : null);
  const when = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);
  const setCondition = (condition: Condition) => tab.change({ condition });
  const toggle = <T,>(list: T[], item: T, on: boolean) => (on ? [...list, item] : list.filter(existing => existing !== item));

  async function runTest() {
    setTesting(true);
    try {
      setTest({ result: await testRule(draft) });
    } catch (error) {
      setTest({ error: failureText(error) });
    }
    setTesting(false);
  }

  async function remove() {
    if (!rule) return;
    try {
      await deleteRule(rule.id);
      setDeleting(false);
      await client.invalidateQueries({ queryKey: ruleKeys.all });
      onDeleted();
    } catch (error) {
      setDeleteError(failureText(error));
    }
  }

  const condition = draft.condition;
  return (
    <section className="editor" aria-label={`Rule: ${title}`}>
      <button type="button" className="button back-button" onClick={onClose}>← Back to rules</button>
      <header className="editor-head"><div className="editor-identity"><h2>{title}</h2></div></header>
      <form className="tab-form tab-form-wide" aria-label="Rule settings" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
        <fieldset className="field-group">
          <legend>Rule</legend>
          <label className="field"><span>Name</span><input value={draft.name} maxLength={120} onChange={event => tab.change({ name: event.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => tab.change({ enabled: event.target.checked })} /> Enabled</label>
        </fieldset>

        <fieldset className="field-group">
          <legend>Cameras</legend>
          <p className="muted field-hint">The rule watches these cameras, and every camera whose policy group is chosen.</p>
          {(groups.data ?? []).length ? (
            <ul className="choice-list member-list" aria-label="Groups">
              {(groups.data ?? []).map(group => (
                <li key={group.id}><label><input type="checkbox" checked={draft.group_ids.includes(group.id)}
                                                  onChange={event => tab.change({ group_ids: toggle(draft.group_ids, group.id, event.target.checked) })} /> Group: {group.name}</label></li>
              ))}
            </ul>
          ) : null}
          <ul className="choice-list member-list" aria-label="Cameras">
            {live.map(camera => (
              <li key={camera.id}><label><input type="checkbox" checked={draft.camera_ids.includes(camera.id)}
                                                 onChange={event => tab.change({ camera_ids: toggle(draft.camera_ids, camera.id, event.target.checked) })} /> {camera.name}</label></li>
            ))}
          </ul>
        </fieldset>

        <fieldset className="field-group">
          <legend>Condition</legend>
          <div className="field-row">
            <label className="field"><span>{condition.type === "tamper" || condition.type === "external_event" ? "When" : "When an object"}</span>
              <select value={condition.type} onChange={event => setCondition(switchCondition(condition, event.target.value as Condition["type"]))}>
                {(Object.keys(CONDITION_LABEL) as Condition["type"][]).map(type => <option key={type} value={type}>{CONDITION_LABEL[type]}</option>)}
              </select>
            </label>
            {condition.type === "line_crossing" ? (
              <label className="field"><span>Direction</span>
                <select value={condition.direction} onChange={event => setCondition({ ...condition, direction: event.target.value as CrossingDirection })}>
                  {(Object.keys(DIRECTION_LABEL) as CrossingDirection[]).map(direction => <option key={direction} value={direction}>{DIRECTION_LABEL[direction]}</option>)}
                </select>
              </label>
            ) : null}
            {condition.type === "loitering" ? (
              <label className="field"><span>For at least (seconds)</span>
                <input type="number" min={1} max={3600} value={Number.isNaN(condition.dwell_seconds) ? "" : condition.dwell_seconds}
                       onChange={event => setCondition({ ...condition, dwell_seconds: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
              </label>
            ) : null}
            {condition.type === "wrong_way" ? (
              <label className="field"><span>Expected direction</span>
                <select value={condition.direction} onChange={event => setCondition({ ...condition, direction: event.target.value as TravelDirection })}>
                  {(Object.keys(TRAVEL_LABEL) as TravelDirection[]).map(direction => <option key={direction} value={direction}>{TRAVEL_LABEL[direction]}</option>)}
                </select>
              </label>
            ) : null}
          </div>
          {condition.type === "external_event" ? (
            <fieldset className="plain">
              <legend className="field-heading">Outside events</legend>
              <p className="muted field-hint">Fires when an event that an administrator mapped to one of this rule's cameras arrives. Leave a box empty to accept any. Set sources up under Settings, Event sources.</p>
              <label className="field"><span>Sources</span>
                <select multiple value={condition.source_ids} onChange={event => setCondition({ ...condition, source_ids: Array.from(event.target.selectedOptions, option => option.value) })}>
                  {(eventSources.data ?? []).map(source => <option key={source.id} value={source.id}>{source.name}</option>)}
                </select>
              </label>
              <label className="field"><span>Event types (comma separated)</span>
                <input value={condition.types.join(", ")} placeholder="door_forced, panic_button" onChange={event => setCondition({ ...condition, types: splitList(event.target.value) })} />
              </label>
              <label className="field"><span>Doors or sensors (comma separated)</span>
                <input value={condition.subjects.join(", ")} placeholder="main-door" onChange={event => setCondition({ ...condition, subjects: splitList(event.target.value) })} />
              </label>
            </fieldset>
          ) : condition.type === "tamper" ? (
            <fieldset className="plain">
              <legend className="field-heading">Kinds of tampering</legend>
              <p className="muted field-hint">Fires once when a camera is confirmed covered, moved or defocused. Turn detection on for each camera under its Tamper tab.</p>
              <div className="segmented">
                {TAMPER_KINDS.map(kind => (
                  <label key={kind}><input type="checkbox" checked={condition.kinds.includes(kind)}
                                           onChange={event => setCondition({ ...condition, kinds: TAMPER_KINDS.filter(item => (item === kind ? event.target.checked : condition.kinds.includes(item))) })} /> {TAMPER_LABEL[kind]}</label>
                ))}
              </div>
            </fieldset>
          ) : (
            <>
              {condition.type === "line_crossing" ? (
                <LineEditor cameraId={previewCamera} line={condition.line} direction={condition.direction} label="Rule line"
                            onChange={line => setCondition({ ...condition, line })} />
              ) : (
                <ZoneEditor cameraId={previewCamera} zones={[condition.zone]} max={1} label="Rule zone" emptyHint="Draw the zone the rule watches."
                            onChange={zones => zones[0] && setCondition({ ...condition, zone: zones[0] })} />
              )}
              {condition.type === "wrong_way" ? (
                <p className="muted field-hint">Fires when an object inside the zone travels a tenth of the frame against the expected direction.</p>
              ) : null}
              <fieldset className="plain">
                <legend className="field-heading">Objects</legend>
                <div className="segmented">
                  {(Object.keys(CLASS_LABEL) as ObjectClass[]).map(item => (
                    <label key={item}><input type="checkbox" checked={draft.classes.includes(item)}
                                             onChange={event => tab.change({ classes: toggle(draft.classes, item, event.target.checked) })} /> {CLASS_LABEL[item]}</label>
                  ))}
                </div>
              </fieldset>
            </>
          )}
        </fieldset>

        <fieldset className="field-group">
          <legend>When</legend>
          <label className="check"><input type="checkbox" checked={draft.schedule !== null}
                                          onChange={event => tab.change({ schedule: event.target.checked ? { timezone: display.data?.time_zone ?? "UTC", intervals: [] } : null })} /> Only at scheduled times</label>
          {draft.schedule ? (
            <>
              <label className="field"><span>Time zone</span><input value={draft.schedule.timezone} autoComplete="off"
                                                                  onChange={event => tab.change({ schedule: { ...draft.schedule!, timezone: event.target.value } })} /></label>
              <ScheduleEditor intervals={intervals} withModes={false}
                              onChange={next => tab.change({ schedule: { ...draft.schedule!, intervals: next.map(({ days, start, end }) => ({ days, start, end })) } })} />
            </>
          ) : <p className="muted field-hint">The rule is always active.</p>}
          <label className="field"><span>Cooldown per camera (seconds)</span>
            <input type="number" min={0} max={86400} value={Number.isNaN(draft.cooldown_seconds) ? "" : draft.cooldown_seconds}
                   onChange={event => tab.change({ cooldown_seconds: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
          </label>
          <p className="muted field-hint">Each object fires a rule at most once. A cooldown also waits this long after an event on a camera before the next.</p>
          <label className="check"><input type="checkbox" checked={draft.pop_into_live} onChange={event => tab.change({ pop_into_live: event.target.checked })} /> Pop alarms into Live</label>
          <p className="muted field-hint">Brings the alarm's camera and its neighbours into Live for people who turned alarm pop-in on, and onto video walls.</p>
        </fieldset>

        <ActionsEditor actions={draft.actions} cameras={live} onChange={actions => tab.change({ actions })} />

        <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={problem} />
      </form>

      <section className="field-group rule-test" aria-label="Test mode">
        <h3 className="field-heading">Test mode</h3>
        <p className="muted field-hint">Replay the last 24 hours of stored detections through this rule as it is now, saved or not. Nothing is created.</p>
        <div className="field-actions">
          <button type="button" className="button" onClick={() => void runTest()} disabled={testing || Boolean(problem)}>{testing ? "Testing…" : "Test against the last 24 hours"}</button>
        </div>
        {test && "error" in test ? <p className="notice" data-tone="bad" role="alert">Test failed: {test.error}.</p> : null}
        {test && "result" in test ? (
          <div role="status">
            <p><strong>{test.result.count === 0 ? "The rule would not have fired." : `The rule would have fired ${test.result.count} time${test.result.count === 1 ? "" : "s"}.`}</strong></p>
            <p className="muted field-hint">{test.result.coverage_note}</p>
            {test.result.actions.length ? (
              <p className="field-hint">Each event would also run, but test mode ran none of them: {test.result.actions.map(action => action.name).join(", ")}.</p>
            ) : null}
            {test.result.events.length ? (
              <table className="zone-table rule-test-table">
                <thead><tr><th scope="col">When</th><th scope="col">Camera</th><th scope="col">Object</th></tr></thead>
                <tbody>
                  {test.result.events.map(event => (
                    <tr key={`${event.track_id ?? event.label}-${event.camera_id}-${event.occurred_at}`}><td>{when(event.occurred_at)}</td><td>{event.camera_name}</td><td>{event.label in CLASS_LABEL ? CLASS_LABEL[event.label as ObjectClass] : TAMPER_LABEL[event.label as TamperKind]}</td></tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </div>
        ) : null}
      </section>

      {rule ? <DeliveryLog ruleId={rule.id} when={when} /> : null}

      {rule ? (
        <>
          <div className="field-actions rule-delete">
            <button type="button" className="button" data-variant="danger" onClick={() => setDeleting(true)}>Delete rule…</button>
          </div>
          <Modal open={deleting} onOpenChange={setDeleting} title={`Delete ${rule.name}?`}
                 description="The rule stops watching its cameras. Its past events are kept."
                 footer={<><button type="button" className="button" onClick={() => setDeleting(false)}>Cancel</button><button type="button" className="button" data-variant="danger" onClick={() => void remove()}>Delete rule</button></>}>
            {deleteError ? <p className="notice" data-tone="bad" role="alert">{deleteError}</p> : null}
          </Modal>
        </>
      ) : null}
    </section>
  );
}
