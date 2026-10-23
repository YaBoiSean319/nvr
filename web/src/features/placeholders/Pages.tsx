import { Link } from "react-router";

/** Workspaces not yet rebuilt point to the current interface instead of pretending to work. */
export function WorkspacePlaceholder({ title, legacyHint }: { title: string; legacyHint: string }) {
  return (
    <section className="placeholder" aria-labelledby="placeholder-title">
      <h1 id="placeholder-title">{title}</h1>
      <p>This workspace is being rebuilt in the new interface.</p>
      <p className="muted">{legacyHint}</p>
      <a className="button" href="/">Open the current interface</a>
    </section>
  );
}

export function AdministratorOnly() {
  return (
    <section className="placeholder" aria-labelledby="forbidden-title">
      <h1 id="forbidden-title">Administrator access required</h1>
      <p>Your account can monitor and investigate, but configuration is reserved for administrators.</p>
      <Link className="button" to="/live">Go to Live</Link>
    </section>
  );
}

export function NotFound() {
  return (
    <section className="placeholder" aria-labelledby="not-found-title">
      <h1 id="not-found-title">Page not found</h1>
      <Link className="button" to="/live">Go to Live</Link>
    </section>
  );
}
