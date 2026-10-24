import * as Tabs from "@radix-ui/react-tabs";
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useCameraGroups, useCameras, useMe, viewKeys } from "@/api/queries";
import { guarded } from "../drafts";
import { failureText } from "../tabDraft";
import { createGroup } from "./api";
import { AiSection, GeneralSection, RetentionSection, ScheduleSection, StorageSection } from "./GroupSections";

type Section = "general" | "schedule" | "retention" | "storage" | "ai";
const SECTION_LABEL: Record<Section, string> = { general: "Name & Members", schedule: "Schedule", retention: "Retention", storage: "Storage", ai: "AI" };

/** A group list and exactly one group editor, one policy section at a time. */
export function GroupsWorkspace() {
  const client = useQueryClient();
  const me = useMe();
  const groups = useCameraGroups();
  const cameras = useCameras();
  const admin = me.data?.role === "administrator";
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [section, setSection] = useState<Section>("general");
  const [name, setName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const selected = (groups.data ?? []).find(group => group.id === selectedId) ?? null;

  async function create(event: FormEvent) {
    event.preventDefault();
    setCreateError(null);
    try {
      const created = await createGroup(name.trim());
      setName("");
      await client.invalidateQueries({ queryKey: viewKeys.groups });
      void guarded(() => { setSelectedId(created.id); setSection("general"); });
    } catch (error) {
      setCreateError(failureText(error));
    }
  }

  return (
    <div className="admin-panes groups-panes" data-detail={selectedId ? "open" : "closed"}>
      <section className="inventory" aria-label="Policy groups">
        {admin ? (
          <form className="inventory-tools" onSubmit={event => void create(event)} aria-label="New group">
            <input className="browser-search" placeholder="New group name" aria-label="New group name" value={name} maxLength={80} onChange={event => setName(event.target.value)} />
            <button type="submit" className="button" disabled={!name.trim()}>Create group</button>
          </form>
        ) : null}
        {createError ? <p className="field-error inventory-empty" role="alert">{createError}</p> : null}
        <p className="inventory-meta">{groups.data?.length ?? 0} groups</p>
        <div className="inventory-scroll">
          {groups.data && !groups.data.length ? <p className="muted inventory-empty">No groups yet.</p> : null}
          <ul className="group-list">
            {(groups.data ?? []).map(group => {
              const policy = (cameras.data ?? []).filter(camera => camera.group_name === group.name).length;
              return (
                <li key={group.id}>
                  <button type="button" className="row-select group-row" aria-current={group.id === selectedId ? "true" : undefined}
                          onClick={() => void guarded(() => { setSelectedId(group.id); setSection("general"); })}>
                    <span>{group.name}</span>
                    <span className="muted">{policy} policy · {group.camera_count} in Live</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      </section>
      <div className="pane-resizer" aria-hidden="true" />
      <div className="editor-pane">
        {selected ? (
          <section className="editor" aria-label={`Policies for ${selected.name}`}>
            <button type="button" className="button back-button" onClick={() => void guarded(() => setSelectedId(null))}>← Back to groups</button>
            <header className="editor-head"><div className="editor-identity"><h2>{selected.name}</h2></div></header>
            <Tabs.Root className="editor-tabs" value={section} onValueChange={value => void guarded(() => setSection(value as Section))}>
              <Tabs.List className="tab-list editor-tab-list" aria-label="Group policies">
                {(Object.keys(SECTION_LABEL) as Section[]).map(key => <Tabs.Trigger key={key} value={key} className="tab">{SECTION_LABEL[key]}</Tabs.Trigger>)}
              </Tabs.List>
              {(Object.keys(SECTION_LABEL) as Section[]).map(key => (
                <Tabs.Content key={key} value={key} className="editor-panel">
                  {section !== key ? null
                    : key === "general" ? <GeneralSection key={selected.id} group={selected} cameras={cameras.data ?? []} admin={admin} onDeleted={() => setSelectedId(null)} />
                    : key === "schedule" ? <ScheduleSection key={selected.id} group={selected} cameras={cameras.data ?? []} admin={admin} />
                    : key === "retention" ? <RetentionSection key={selected.id} group={selected} cameras={cameras.data ?? []} admin={admin} />
                    : key === "storage" ? <StorageSection key={selected.id} group={selected} cameras={cameras.data ?? []} admin={admin} />
                    : <AiSection key={selected.id} group={selected} cameras={cameras.data ?? []} admin={admin} />}
                </Tabs.Content>
              ))}
            </Tabs.Root>
          </section>
        ) : <div className="editor-empty"><p><strong>Select a group to view its policies.</strong></p></div>}
      </div>
    </div>
  );
}
