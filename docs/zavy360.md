# Zavy360 booking backend — Thursday investigation

Ava's diary must go through `BookingProvider` (`src/booking.py`). Agent logic never hardcodes slots.

## What we found (2026-09-15)

Public surface is the hosted booking site, not a documented REST API:

- Online booking URLs: `https://bookings.zavy360.com/booking/{practice-subdomain}`
- Setup is inside Zavy 360: Settings → Calendar → Online Bookings (links + iframe embed)
- Help: [Online Bookings Setup Guide](https://help.zavy.com/en/articles/5222719-online-bookings-setup-guide)

**API platform is early access only.** March 2026 v3.0 release notes:

> The Zavy 360 API is now available in limited early access… programmatic access to patients, appointments, and more. Early access participants will receive API documentation, sandbox access, and direct support… contact support to join the waitlist.

Source: [What's New — March 2026 v3.0](https://help.zavy.com/en/articles/13958218-what-s-new-march-2026-v3-0)

There is a **Healthengine** integration (availability sync ~5 minutes), which is a partner API, not something we can call as Ava on Thursday without practice credentials.

Headless scraping of `bookings.zavy360.com` was ruled out: fragile, likely ToS-hostile, and too risky for a live owner demo.

## Thursday decision

| Env | What runs |
|-----|-----------|
| `BOOKING_PROVIDER=memory` (default with `PRACTICE_SOFTWARE=mock`) | `MemoryBookingProvider` — seeded week of availability for Shellharbour, Dapto, and Woonona |
| `BOOKING_PROVIDER=zavy360` | `Zavy360BookingProvider` stub — returns `zavy360_unavailable`, never invents slots. If `ZAVY360_API_URL` + `ZAVY360_API_KEY` are later provided, it will attempt HTTP and still fail closed |

`BOOKING_FORCE_TIMEOUT=1` exercises the timeout recovery path (scenario 9).

## After Thursday

1. Ask the practice to request Zavy API early access (`support@zavy.com`).
2. Fill `ZAVY360_API_URL` / `ZAVY360_API_KEY` once docs exist.
3. Keep the same `BookingProvider` methods: `check_availability`, `book_appointment`, `reschedule_appointment`, `cancel_appointment` (with `$50` window), `lookup_patient`, `take_message`.
