import * as Menu from "@radix-ui/react-dropdown-menu";
import { useSavePreferences, useSignOut } from "@/api/queries";
import { guarded } from "@/features/admin/drafts";
import type { Me, ThemePreference } from "@/api/types";
import { applyTheme } from "@/lib/appearance";

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "Match system" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export function UserMenu({ me, theme }: { me: Me; theme: ThemePreference }) {
  const save = useSavePreferences();
  const signOut = useSignOut();
  function choose(value: string) {
    const next = value as ThemePreference;
    applyTheme(next);
    save.mutate(next);
  }
  return (
    <Menu.Root>
      <Menu.Trigger className="button user-trigger" data-variant="ghost" aria-label={`Account menu for ${me.username}`}>
        <span className="user-initial" aria-hidden="true">{me.username.charAt(0).toUpperCase()}</span>
        <span className="user-name">{me.username}</span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content className="menu" align="end" sideOffset={6}>
          <Menu.Label className="menu-label">
            {me.username} · {me.role === "administrator" ? "Administrator" : "Operator"}
          </Menu.Label>
          <Menu.Separator className="menu-separator" />
          <Menu.Label className="menu-label">Appearance</Menu.Label>
          <Menu.RadioGroup value={theme} onValueChange={choose}>
            {THEMES.map(option => (
              <Menu.RadioItem key={option.value} value={option.value} className="menu-item">
                <Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>
                {option.label}
              </Menu.RadioItem>
            ))}
          </Menu.RadioGroup>
          {save.isError ? <p className="menu-label error">Theme could not be saved.</p> : null}
          <Menu.Separator className="menu-separator" />
          <Menu.Item className="menu-item" onSelect={() => void guarded(() => signOut.mutate())}>Sign out</Menu.Item>
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
