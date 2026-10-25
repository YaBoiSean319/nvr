import { useQueryClient } from "@tanstack/react-query";
import { keys, useSignOut } from "@/api/queries";
import type { Me } from "@/api/types";
import { AuthScreen } from "./AuthScreen";
import { SecurityPanel } from "./SecurityPanel";

/** Shown instead of the application to someone who must set up a second factor before going further. */
export function EnrolmentRequired({ me }: { me: Me }) {
  const client = useQueryClient();
  const signOut = useSignOut();
  return (
    <AuthScreen title="Set up a second factor">
      <p className="muted">{me.username}, your administrator requires a second factor to sign in. Set one up now to continue.</p>
      <SecurityPanel onChange={() => void client.invalidateQueries({ queryKey: keys.me })} />
      <button type="button" className="button auth-wide" data-variant="ghost" onClick={() => signOut.mutate()}>Sign out</button>
    </AuthScreen>
  );
}
