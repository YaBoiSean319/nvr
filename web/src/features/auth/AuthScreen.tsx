import type { ReactNode } from "react";
import { usePublicBranding } from "@/api/queries";
import { BrandMark } from "@/components/BrandMark";

export function AuthScreen({ title, children }: { title: string; children: ReactNode }) {
  const branding = usePublicBranding();
  return (
    <main className="auth-screen">
      <section className="auth-panel" aria-labelledby="auth-title">
        <div className="auth-brand">
          <BrandMark branding={branding.data} size={36} />
          <span className="auth-app-name">{branding.data?.app_name ?? "Workplace NVR"}</span>
        </div>
        <h1 id="auth-title">{title}</h1>
        {children}
      </section>
    </main>
  );
}
