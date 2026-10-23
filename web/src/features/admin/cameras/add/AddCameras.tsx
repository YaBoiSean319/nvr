import * as Tabs from "@radix-ui/react-tabs";
import { useState } from "react";
import { CsvImport } from "./CsvImport";
import { Discover } from "./Discover";
import { ManualAdd } from "./ManualAdd";

type Mode = "discover" | "manual" | "csv";

/** Discover, Manual Add and CSV Import, each kept apart from the inventory. */
export function AddCameras() {
  const [mode, setMode] = useState<Mode>("discover");
  return (
    <Tabs.Root className="add-cameras" value={mode} onValueChange={value => setMode(value as Mode)}>
      <Tabs.List className="tab-list add-modes" aria-label="Ways to add cameras">
        <Tabs.Trigger className="tab" value="discover">Discover</Tabs.Trigger>
        <Tabs.Trigger className="tab" value="manual">Manual Add</Tabs.Trigger>
        <Tabs.Trigger className="tab" value="csv">CSV Import</Tabs.Trigger>
      </Tabs.List>
      <Tabs.Content className="add-panel" value="discover">{mode === "discover" ? <Discover /> : null}</Tabs.Content>
      <Tabs.Content className="add-panel add-panel-scroll" value="manual">{mode === "manual" ? <ManualAdd /> : null}</Tabs.Content>
      <Tabs.Content className="add-panel" value="csv">{mode === "csv" ? <CsvImport /> : null}</Tabs.Content>
    </Tabs.Root>
  );
}
