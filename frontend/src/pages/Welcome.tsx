import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CheckCircle2, Sparkles, ArrowRight, Building2, Users2, SlidersHorizontal,
} from "lucide-react";
import { api, getDeduped } from "../api/client";
import { currentMga, getUser, setTenantBrand } from "../auth";
import { fileToLogoDataUrl, initials } from "../branding";
import Button from "../components/ui/Button";
import { Field, Select, TextInput } from "../components/ui/Field";
import { InfoTip } from "../components/InfoTip";
import CountryOptions from "../components/CountryOptions";

type Status = {
  tenant_ready: boolean; parties_ready: boolean;
  contract_ready: boolean; bordereau_ready: boolean; bdx_ready: boolean;
  needs_onboarding: boolean;
};
type Tenant = {
  legal_name?: string; tenant_type?: string;
  address?: any; currency?: string; logo?: string | null;
};
const CURRENCIES = [
  "USD", "EUR", "GBP", "CAD", "AUD", "INR", "JPY", "CHF", "SGD", "AED",
];
type Party = { id: number; legal_name: string };

const TENANT_TYPES = ["mga", "mgu", "broker", "tpa", "carrier", "reinsurer"];
const PARTY_TYPES = ["carrier"];
const PARTY_TYPE_LABEL: Record<string, string> = {
  insurer: "Carrier"
};

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

  // Step 2 — parties ---------------------------------------------------------
  const [parties, setParties] = useState<Party[]>([]);
  const [newParty, setNewParty] = useState<{
    legal_name: string; party_type: string; scope: string; domicile_country: string;
  }>({
    legal_name: "", party_type: "carrier", scope: "tenant", domicile_country: "",
  });

  async function reload() {
    const [s, t, p] = await Promise.all([
      api.get<Status>(`/onboarding/status`, { params: { mga } }),
      getDeduped<Tenant>(`/tenants/${mga}`),
      api.get(`/parties`, { params: { mga } }),
    ]);
    setStatus(s.data);
    setTenant(t.data);
    setParties(p.data.items ?? []);
  }
  useEffect(() => { void reload(); }, [mga]);

  function patchTenant<K extends keyof Tenant>(k: K, v: Tenant[K]) {
    setTenant(prev => prev ? { ...prev, [k]: v } : { [k]: v } as Tenant);
  }

  // The "active" step is the first not-yet-done one. The user can always skip.
  const activeStep = !status ? 1
    : !status.tenant_ready ? 1
    : !status.parties_ready ? 2
    : !status.bordereau_ready ? 3
    : 4;

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

  async function addParty() {
    if (!newParty.legal_name.trim()) return;
    setErr(null); setBusy(true);
    try {
      await api.post<Party>(`/parties`,
        {
          legal_name: newParty.legal_name,
          party_type: newParty.party_type,
          scope: newParty.scope,
          domicile_country: newParty.domicile_country || undefined,
        },
        { params: { mga } });
      setNewParty({ legal_name: "", party_type: "carrier", scope: "tenant", domicile_country: "" });
      await reload();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not add party.");
    } finally { setBusy(false); }
  }

  // --- render --------------------------------------------------------------

  const tenantDone    = !!status?.tenant_ready;
  const partiesDone   = !!status?.parties_ready;
  const bordereauDone = !!status?.bordereau_ready;

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
            Three quick steps to set up your workspace. You can skip and do
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
                    <option value="">Select…</option>
                    {TENANT_TYPES.map(t =>
                      <option key={t} value={t}>{t.toUpperCase()}</option>)}
                  </Select>
                </Field>
                <Field label="Account code (read-only)">
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

              <div className="flex items-center gap-3">
                <Button onClick={saveTenant}
                  disabled={busy
                    || !tenant?.legal_name?.trim()
                    || !tenant?.tenant_type
                    || !tenant?.currency}>
                  <Building2 size={14} />
                  Save Organization
                </Button>
                {err && <span className="text-sm text-danger">{err}</span>}
              </div>
            </div>
          )}
        </Step>

        <Spacer />

        {/* Step 2 — Trading Partner */}
        <Step n={2} title="Carrier" done={partiesDone}
          locked={activeStep < 2}
          hint="Add your first carrier.">
          {partiesDone ? (
            <p className="text-sm text-emerald-700 flex items-center gap-2">
              <CheckCircle2 size={16} /> {parties.length} carrier{parties.length === 1 ? "" : "s"} added.
            </p>
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Legal name *">
                  <TextInput value={newParty.legal_name}
                    onChange={e => setNewParty({ ...newParty, legal_name: e.target.value })}
                    placeholder="e.g. Pinnacle Insurance Co." />
                </Field>
                <Field label="Type *">
                  <Select value={newParty.party_type}
                    onChange={e => setNewParty({ ...newParty, party_type: e.target.value })}>
                    {PARTY_TYPES.map(t =>
                      <option key={t} value={t}>{PARTY_TYPE_LABEL[t] ?? t}</option>)}
                  </Select>
                </Field>
                <Field label="Country">
                  <Select value={newParty.domicile_country}
                    onChange={e => setNewParty({ ...newParty, domicile_country: e.target.value })}>
                    <option value="" disabled>Please select the carrier's country</option>
                    <CountryOptions />
                  </Select>
                </Field>
              </div>

              <div className="flex items-center gap-3">
                <Button onClick={addParty}
                  disabled={busy || !newParty.legal_name.trim()}>
                  <Users2 size={14} /> + Add Carrier
                </Button>
                <InfoTip text="You can also create carriers directly inside Bordereau Setup." />
              </div>
            </div>
          )}
        </Step>

        <Spacer />

        {/* Step 3 — Bordereau setup (input + output + contract, all in one place).
            Optional here: it can also be finished anytime from the Bordereau
            Setup screen in the sidebar, so it doesn't block leaving onboarding. */}
        <Step n={3} title="Bordereau Setup" done={bordereauDone}
          locked={activeStep < 3}
          hint="Set up your input template, output template, and contract in one place. This step is optional—you can also configure them later from the sidebar.">
          {bordereauDone ? (
            <p className="text-sm text-emerald-700 flex items-center gap-2">
              <CheckCircle2 size={16} /> Bordereau mapping configured &amp; activated.
            </p>
          ) : (
            <div className="space-y-3">
              <div className="rounded-md bg-blue-50 border border-blue-200 px-3 py-2 text-xs text-blue-800">
                <strong>All in one place:</strong>
                <ul className="mt-1.5 space-y-1 list-disc pl-4">
                  <li>Upload input sample, output template and contract together.</li>
                  <li>Kavachio maps input → output with confidence scores.</li>
                  <li>Review, adjust and activate.</li>
                  <li>Create carrier &amp; program right here.</li>
                </ul>
              </div>
              <Button onClick={() => nav("/direct/setup")}>
                <SlidersHorizontal size={14} /> Open Bordereau Setup <ArrowRight size={14} />
              </Button>
            </div>
          )}
        </Step>

        {err && <div className="mt-4 text-sm text-danger">{err}</div>}

        <div className="mt-10 flex items-center justify-between">
          <button onClick={() => { api.post(`/onboarding/skip`, null, { params: { mga } }).catch(() => {}); nav("/home"); }}
            className="text-sm text-ink-muted hover:text-ink">
            Skip for Now →
          </button>
          {/* Organization + Trading Partner are mandatory; Bordereau Setup is
              not — it's reachable anytime from the sidebar — so this CTA
              appears once the mandatory steps are done, not all three. */}
          {tenantDone && partiesDone && (
            <Button onClick={() => nav("/home")}>
              <Sparkles size={14} /> Go to Dashboard
            </Button>
          )}
        </div>
      </div>
    </div>
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
