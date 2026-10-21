# Incident tracking

The authenticated `GET /api/incidents` endpoint lists active, acknowledged, and recovered incidents, newest observations first. Add `?state=active`, `?state=acknowledged`, or `?state=recovered` to filter the list. Entries include stable IDs, first/last observation times, recurrence counts, current fault state, affected camera/resource details, and an event history.

Operators and administrators may acknowledge an active incident with `POST /api/incidents/{incident_id}/acknowledge`. Acknowledgement records the actor in incident history and changes the displayed state to `acknowledged`; it does not change `fault_active` or suppress continued observations. Recovered incidents cannot be acknowledged. Repeated observations update the existing record, while a fault that returns after recovery reuses its stable incident ID, clears the old acknowledgement, increments the recurrence count, and adds a recurrence event.

The recording supervisor raises incidents for failed or uncertain recording coverage and recovers them when the coverage state clears. Storage health checks raise grouped incidents for unhealthy configured volumes, including assigned cameras, and resolve them when the volume is healthy again. Storage is checked at startup and every 30 seconds; administrators can also trigger an immediate check from the Storage status view/API.
