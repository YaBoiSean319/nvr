import { Link } from "react-router";

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
