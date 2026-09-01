import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CheckCircle2, Sparkles, ArrowRight, Building2, Users2, Layers,
  SlidersHorizontal,
} from "lucide-react";
import { api, getDeduped } from "../api/client";
import { currentMga, getUser, setTenantBrand } from "../auth";
import { fileToLogoDataUrl, initials } from "../branding";
import Button from "../components/ui/Button";
import { Field, Select, TextInput } from "../components/ui/Field";
import { InfoTip } from "../components/InfoTip";
import CountryOptions from "../components/CountryOptions";

type Status = {
  tenant_ready: boolean; programs_ready?: boolean; parties_ready: boolean;
  contract_ready: boolean; bordereau_ready: boolean; bdx_ready: boolean;
  has_program?: boolean; needs_onboarding: boolean;
};
type Tenant = {
  legal_name?: string; tenant_type?: string;
  address?: any; currency?: string; logo?: string | null;
};
const CURRENCIES = [
  "USD", "EUR", "GBP", "CAD", "AUD", "INR", "JPY", "CHF", "SGD", "AED",
];
type Programme = { id: number; name: string; bdx_frequency?: string | null };

const TENANT_TYPES = ["carrier"];
const BDX_FREQUENCIES = ["monthly", "quarterly", "annually"];

export default function Welcome() {
  const nav = useNavigate();
  const user = getUser();
  const mga = currentMga();

  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  // Step 1 — tenant ----------------------------------------------------------
  const [tenant, setTenant] = useState<Tenant | null>(null);

  // Step 2 — programmes ------------------------------------------------------
  // A programme is the carrier's own book. Brokers are a relationship rather
  // than part of setting yourself up, so they are added later from the Brokers
  // screen — where the link to a programme is made.
  const [programmes, setProgrammes] = useState<Programme[]>([]);
  const [newProgramme, setNewProgramme] = useState<{
    name: string; bdx_frequency: string;
  }>({ name: "", bdx_frequency: "monthly" });

  async function reload() {
    const [s, t, pr] = await Promise.all([
      api.get<Status>(`/onboarding/status`, { params: { mga } }),
      getDeduped<Tenant>(`/tenants/${mga}`),
      api.get<Programme[]>(`/programs`, { params: { mga } }),
    ]);
    setStatus(s.data);
    setTenant({ ...t.data, tenant_type: t.data?.tenant_type || "carrier" });
    setProgrammes(pr.data ?? []);
  }
  useEffect(() => { void reload(); }, [mga]);

  function patchTenant<K extends keyof Tenant>(k: K, v: Tenant[K]) {
    setTenant(prev => prev ? { ...prev, [k]: v } : { [k]: v } as Tenant);
  }

  const tenantDone     = !!status?.tenant_ready;
  // Tolerate a backend that predates `programs_ready` by falling back to the
  // long-standing has_program key.
  const programmesDone = !!(status?.programs_ready ?? status?.has_program);
  const allDone        = tenantDone && programmesDone;

  // The "active" step is the first not-yet-done one. The user can always skip.
  const activeStep = !status ? 1
    : !tenantDone ? 1
    : !programmesDone ? 2
    : 3;

  // --- actions -------------------------------------------------------------

  async function onLogoFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!f) return;
    setErr(null);
    try { patchTenant("logo", await fileToLogoDataUrl(f)); }
    catch (er: any) { setErr(er?.message ?? "Could not read that image."); }
  }

  async function saveTenant() {
    setErr(null); setToast(null); setBusy(true);
    try {
      await api.put(`/tenants/${mga}`, {
        legal_name: tenant?.legal_name, tenant_type: tenant?.tenant_type,
        address: tenant?.address, currency: tenant?.currency,
        logo: tenant?.logo ?? null,
      });
      // Push the new logo/name to the sidebar co-brand immediately.
      setTenantBrand({ mga, legal_name: tenant?.legal_name, logo: tenant?.logo ?? null });
      await reload();
      setToast("Organization saved — moving to step 2.");
      setTimeout(() => setToast(null), 3000);
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      setErr(typeof detail === "string" ? detail
        : detail?.message ?? e?.message ?? "Save failed.");
    } finally { setBusy(false); }
  }

  async function addProgramme() {
    if (!newProgramme.name.trim()) return;
    setErr(null); setBusy(true);
    try {
      await api.post<Programme>(`/programs`,
        {
          name: newProgramme.name.trim(),
          bdx_frequency: newProgramme.bdx_frequency,
          status: "active",
        },
        { params: { mga } });
      setNewProgramme({ name: "", bdx_frequency: "monthly" });
      await reload();
      setToast("Programme created.");
      setTimeout(() => setToast(null), 3000);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not create the programme.");
    } finally { setBusy(false); }
  }


  // --- render --------------------------------------------------------------

  return (
    <div className="min-h-screen bg-bg">
      {toast && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 z-50
          bg-emerald-600 text-white text-sm font-medium px-4 py-2 rounded-md shadow-lg">
          {toast}
        </div>
      )}
      <div className="max-w-3xl mx-auto py-12 px-6">
        <header className="mb-8">
          <div className="text-sm text-ink-muted mb-1">Organization: {mga}</div>
          <h1 className="text-3xl font-semibold text-navy tracking-tight">
            Welcome, {user?.full_name?.split(" ")[0] ?? "there"} 👋
          </h1>
          <p className="text-ink-muted mt-2">
            Two quick steps to set up your workspace. You can skip and do
            these later from the sidebar.
          </p>
        </header>

        {/* Step 1 — Organization */}
        <Step n={1} title="Organization" done={tenantDone}
          locked={activeStep < 1}
          hint="Confirm your organization's legal name, type, and currency.">
          {tenantDone ? (
            <p className="text-sm text-emerald-700 flex items-center gap-2">
              <CheckCircle2 size={16} /> Organization configured.
            </p>
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Legal name *">
                  <TextInput value={tenant?.legal_name ?? ""}
                    onChange={e => patchTenant("legal_name", e.target.value)} />
                </Field>
                <Field label="Organization type *">
                  <Select value={tenant?.tenant_type ?? ""}
                    onChange={e => patchTenant("tenant_type", e.target.value)}>
                    {/* A tenant provisioned as something else keeps its own
                        value, so opening this wizard never rewrites it. */}
                    {tenant?.tenant_type && !TENANT_TYPES.includes(tenant.tenant_type) && (
                      <option value={tenant.tenant_type}>
                        {tenant.tenant_type.toUpperCase()}
                      </option>
                    )}
                    {TENANT_TYPES.map(t =>
                      <option key={t} value={t}>{t.toUpperCase()}</option>)}
                  </Select>
                </Field>
                <Field label="Workspace ID (read-only)">
                  <TextInput value={mga} readOnly className="bg-surface-2" />
                </Field>
                <Field label="Currency *">
                  <Select value={tenant?.currency ?? ""}
                    onChange={e => patchTenant("currency", e.target.value)}>
                    <option value="">Select…</option>
                    {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </Select>
                </Field>
              </div>

              {/* Organization logo — optional; shows next to the Kavachio mark
                  in the sidebar. */}
              <Field label="Organization logo">
                <div className="flex items-center gap-3">
                  {/* Preview on a dark swatch, matching the sidebar. */}
                  {tenant?.logo
                    ? <img src={tenant.logo} alt="Organization logo"
                        className="w-12 h-12 rounded-lg object-contain"
                        style={{ background: "#131a29", border: "1px solid rgba(255,255,255,.08)" }} />
                    : <div className="w-12 h-12 rounded-lg grid place-items-center text-white text-sm font-bold"
                        style={{ background: "linear-gradient(135deg,#077282,#03A2A6)" }}>
                        {initials(tenant?.legal_name)}
                      </div>}
                  <label className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium
                    rounded-md bg-white border border-border text-ink hover:bg-surface-2 cursor-pointer">
                    {tenant?.logo ? "Replace Logo" : "Upload Logo"}
                    <input type="file" accept="image/*" className="hidden" onChange={onLogoFile} />
                  </label>
                  {tenant?.logo && (
                    <button type="button" onClick={() => patchTenant("logo", null)}
                      className="text-sm text-ink-muted hover:text-danger">Remove</button>
                  )}
                  <span className="text-[11px] text-ink-soft">Shows on a dark sidebar — a light or transparent logo works best. PNG, SVG or JPG.</span>
                </div>
              </Field>

              {(() => {
                const addr: Record<string, string> =
                  (tenant && typeof tenant.address === "object" && tenant.address)
                    ? (tenant.address as Record<string, string>)
                    : {};
                const setAddr = (k: string, v: string) =>
                  patchTenant("address", { ...addr, [k]: v });
                return (
                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">
                      Registered Address
                    </h3>
                    <div className="grid grid-cols-2 gap-3">
                      {(["line1", "line2", "city", "state", "zip"] as const).map(k => (
                        <Field key={k} label={k}>
                          <TextInput value={addr[k] ?? ""}
                            onChange={(e) => setAddr(k, e.target.value)} />
                        </Field>
                      ))}
                      <Field label="country">
                        <Select value={addr.country ?? ""}
                          onChange={(e) => setAddr("country", e.target.value)}>
                          <option value="">Select country…</option>
                          <CountryOptions />
                        </Select>
                      </Field>
                    </div>
                  </div>
                );
              })()}

              {/* A disabled button with no reason is a dead end — the user
                  has to guess which of three required fields is empty. Name it. */}
              {(() => {
                const missing = [
                  !tenant?.legal_name?.trim() && "Legal name",
                  !tenant?.tenant_type && "Organization type",
                  !tenant?.currency && "Currency",
                ].filter(Boolean) as string[];
                return (
                  <div className="flex items-center gap-3">
                    <Button onClick={saveTenant} disabled={busy || missing.length > 0}>
                      <Building2 size={14} />
                      Save Organization
                    </Button>
                    {missing.length > 0 && (
                      <span className="text-sm text-ink-muted">
                        Still needed: {missing.join(", ")}
                      </span>
                    )}
                    {err && <span className="text-sm text-danger">{err}</span>}
                  </div>
                );
              })()}
            </div>
          )}
        </Step>

        <Spacer />

        {/* Step 2 — Programme. The carrier's book is divided into programmes,
            and every broker and contract below hangs off one, so it comes
            before the broker rather than after it. */}
        <Step n={2} title="Programme" done={programmesDone}
          locked={activeStep < 2}
          hint="Create your first programme — the book a broker will produce into.">
          {programmesDone ? (
            <p className="text-sm text-emerald-700 flex items-center gap-2">
              <CheckCircle2 size={16} />
              {programmes.length} programme{programmes.length === 1 ? "" : "s"} created
              {programmes.length ? ` — ${programmes.map(p => p.name).join(", ")}` : ""}.
            </p>
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Programme name *">
                  <TextInput value={newProgramme.name}
                    onChange={e => setNewProgramme({ ...newProgramme, name: e.target.value })}
                    placeholder="e.g. Coastal Property" />
                </Field>
                <Field label="Bordereau frequency">
                  <Select value={newProgramme.bdx_frequency}
                    onChange={e => setNewProgramme({ ...newProgramme, bdx_frequency: e.target.value })}>
                    {BDX_FREQUENCIES.map(f =>
                      <option key={f} value={f}>{f[0].toUpperCase() + f.slice(1)}</option>)}
                  </Select>
                </Field>
              </div>

              <div className="flex items-center gap-3">
                <Button onClick={addProgramme}
                  disabled={busy || !newProgramme.name.trim()}>
                  <Layers size={14} /> + Add Programme
                </Button>
                <InfoTip text="How often you expect a bordereau on this programme. You can change it later, and set exact due dates from the programme's calendar." />
              </div>
            </div>
          )}
        </Step>


        {/* Finished — what exists now, and what the carrier does next. This
            replaces the old jump straight into Bordereau Setup: that screen
            needs a contract in hand, which is two steps further on. */}
        {allDone && (
          <>
            <Spacer />
            <FinishPanel
              carrier={tenant?.legal_name || mga}
              programmes={programmes}
              onBrokers={() => nav("/brokers")}
              onDashboard={() => nav("/home")}
              onBordereau={() => nav("/direct/setup")}
            />
          </>
        )}

        {err && <div className="mt-4 text-sm text-danger">{err}</div>}

        <div className="mt-10 flex items-center justify-between">
          <button onClick={() => { api.post(`/onboarding/skip`, null, { params: { mga } }).catch(() => {}); nav("/home"); }}
            className="text-sm text-ink-muted hover:text-ink">
            Skip for Now →
          </button>
          {/* Once everything is done the finish panel carries the primary
              action, so this duplicate CTA would only compete with it. */}
          {!allDone && tenantDone && programmesDone && (
            <Button onClick={() => nav("/home")}>
              <Sparkles size={14} /> Go to Dashboard
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

/** The end of onboarding: what the carrier has set up, then the one thing to
 *  do next. Adding brokers is that next thing rather than a step above,
 *  because a broker is a relationship with another firm — it exists when
 *  there is one, not because a wizard demanded a name. */
function FinishPanel({ carrier, programmes, onBrokers, onDashboard, onBordereau }: {
  carrier: string;
  programmes: Programme[];
  onBrokers: () => void;
  onDashboard: () => void;
  onBordereau: () => void;
}) {
  const rows: { label: string; value: string }[] = [
    { label: "Carrier", value: carrier },
    {
      label: programmes.length === 1 ? "Programme" : "Programmes",
      value: programmes.map(p => p.name).join(", ") || "—",
    },
  ];

  const next: { title: string; body: string }[] = [
    {
      title: "Add the brokers you work with",
      body: "Putting a broker on a programme is what lets them produce into it. Do that from the Brokers screen, when the relationship is real.",
    },
    {
      title: "Invite the broker's admin",
      body: "They get their own login and can upload the contract for your programme.",
    },
    {
      title: "Approve the contract when it arrives",
      body: "A contract a broker uploads waits for you. Approving it is what makes it live.",
    },
    {
      title: "Set up the bordereau mapping",
      body: "Once a contract is live, map their columns to your output layout.",
    },
  ];

  return (
    <section className="card overflow-hidden">
      <div className="px-6 py-5 flex items-start gap-3"
        style={{ background: "linear-gradient(135deg,#077282,#03A2A6)" }}>
        <div className="w-9 h-9 rounded-full bg-white/15 grid place-items-center shrink-0">
          <Sparkles size={18} className="text-white" />
        </div>
        <div>
          <h2 className="text-base font-semibold text-white">Your workspace is ready</h2>
          <p className="text-sm text-white/80 mt-0.5">
            Your organization and your first programme are set up. Here is what you have.
          </p>
        </div>
      </div>

      <div className="px-6 py-5 space-y-5">
        <dl className="divide-y divide-border rounded-md border border-border overflow-hidden">
          {rows.map(r => (
            <div key={r.label} className="grid grid-cols-3 gap-3 px-4 py-2.5 bg-white">
              <dt className="text-xs font-semibold uppercase tracking-wide text-ink-muted self-center">
                {r.label}
              </dt>
              <dd className="col-span-2 text-sm text-ink">{r.value}</dd>
            </div>
          ))}
        </dl>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-3">
            What happens next
          </h3>
          <ol className="space-y-3">
            {next.map((n, i) => (
              <li key={n.title} className="flex gap-3">
                <span className="w-6 h-6 rounded-full bg-surface-2 text-ink-muted
                  text-xs font-semibold grid place-items-center shrink-0 mt-0.5">
                  {i + 1}
                </span>
                <div>
                  <div className="text-sm font-medium text-ink">{n.title}</div>
                  <div className="text-xs text-ink-muted mt-0.5">{n.body}</div>
                </div>
              </li>
            ))}
          </ol>
        </div>

        <div className="flex flex-wrap items-center gap-4 pt-1">
          <Button onClick={onBrokers}>
            <Users2 size={14} /> Add your brokers <ArrowRight size={14} />
          </Button>
          <button onClick={onDashboard}
            className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink">
            <Sparkles size={13} /> Go to Dashboard
          </button>
          <button onClick={onBordereau}
            className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink">
            <SlidersHorizontal size={13} /> Set up bordereau mapping
          </button>
        </div>
      </div>
    </section>
  );
}

function Step({ n, title, done, locked, hint, children }: {
  n: number; title: string; done: boolean; locked: boolean;
  hint: string; children: React.ReactNode;
}) {
  return (
    <section className={`card p-6 transition
      ${done ? "border-emerald-200 bg-emerald-50/40" : ""}
      ${locked ? "opacity-55 pointer-events-none" : ""}`}>
      <header className="flex items-start gap-3 mb-3">
        <div className={`w-7 h-7 rounded-full flex items-center justify-center text-sm font-semibold shrink-0
          ${done ? "bg-emerald-100 text-emerald-700"
            : locked ? "bg-surface-2 text-ink-soft"
            : "bg-navy text-white"}`}>
          {done ? <CheckCircle2 size={16} /> : n}
        </div>
        <div>
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-xs text-ink-muted mt-0.5">{hint}</p>
        </div>
      </header>
      <div className="pl-10">{children}</div>
    </section>
  );
}

function Spacer() { return <div className="h-3" />; }
