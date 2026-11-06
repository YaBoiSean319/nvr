import * as Tabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

export interface SectionTab { id: string; label: string }

/** One settings page split into sections that show one at a time, so a long page does not have to be scrolled. */
export function SectionTabs({ label, tabs, children }: { label: string; tabs: SectionTab[]; children: ReactNode }) {
  return (
    <Tabs.Root className="section-tabs" defaultValue={tabs[0]?.id}>
      <Tabs.List className="section-tab-list" aria-label={label}>
        {tabs.map(tab => <Tabs.Trigger key={tab.id} value={tab.id} className="status-tab">{tab.label}</Tabs.Trigger>)}
      </Tabs.List>
      {children}
    </Tabs.Root>
  );
}

export function SectionPanel({ id, children }: { id: string; children: ReactNode }) {
  return <Tabs.Content value={id} className="settings-stack">{children}</Tabs.Content>;
}
