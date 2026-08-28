import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Sparkles, Upload, Plus, Loader2, CheckCircle2, ArrowRight } from "lucide-react";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import Button from "../components/ui/Button";
import { Field, Select, TextInput } from "../components/ui/Field";
import { PageBody, PageHeader } from "../components/Layout";
import PipelineStepper from "../components/PipelineStepper";
import { LoadingOverlay } from "../components/Busy";

type Party = {
  id: number; legal_name: string; dba_name?: string; party_type: string; is_active: boolean;
};
type Program = {
  id: number; name: string; party_id?: number | null; lead_carrier?: string; admin_party?: string;
  bdx_frequency?: string; business_segment?: string; product_line?: string;
  distribution_channel?: string; territory?: string; status?: string;
  source_contract_file?: string; commercial_terms?: any;
};
const BDX_FREQUENCIES: [string, string][] = [
  ["monthly", "Monthly"], ["quarterly", "Quarterly"],
  ["semi-annual", "Semi-Annual"], ["annual", "Annual"],
];
type Contract = {
  id: number; filename: string; status: string;
  extracted: any; output_template_id?: number | null;
  schedule_key?: string | null; created_at: string | null;
};

const CONTRACT_ACTIVE = "active";
const CONTRACT_SUPERSEDED = "superseded";
type PartyForm = { legal_name: string; party_type: string; dba_name: string };
const PARTY_TYPES = ["carrier"];


export default function Programs() {
  const mga = currentMga();
  const navigate = useNavigate();
  const [parties, setParties] = useState<Party[]>([]);
  const [selectedPartyId, setSelectedPartyId] = useState<string>("");
  const [showCreateParty, setShowCreateParty] = useState(false);
  const [partyForm, setPartyForm] = useState<PartyForm>({ legal_name: "", party_type: "carrier", dba_name: "" });
  const [partyBusy, setPartyBusy] = useState(false);

  const [items, setItems] = useState<Program[]>([]);
  const [sel, setSel] = useState<Program | null>(null);
  const [contracts, setContracts] = useState<Contract[]>([]);
  // Phase 1: which schedule this uploaded contract covers (blank = legacy single
  // active contract per program). Lets one program hold many contracts.
  const [scheduleKey, setScheduleKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [activatingContract, setActivatingContract] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [creatingName, setCreatingName] = useState<string>("");
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Output Template the active/latest contract is linked to. A new contract
  // version is uploaded against this same template. null = no setup yet → the
  // user must run first-time setup (output template + contract) on /outputs.
  const [linkedTemplateId, setLinkedTemplateId] = useState<number | null>(null);

  function loadParties() {
    api.get<{ items: Party[] }>(`/parties`, { params: { mga } })
      .then(r => {
        const items = r.data.items;
        setParties(items);
        // Auto-select the first party so the user lands on their data immediately
        if (items.length > 0) {
          setSelectedPartyId(prev => {
            if (prev) return prev;   // keep user's manual selection
            loadList(items[0].id);
            return String(items[0].id);
          });
        }
      });
  }
  useEffect(() => { loadParties(); }, [mga]);

  function loadList(partyId?: number, keepSelId?: number) {
    const pid = partyId ?? (selectedPartyId ? Number(selectedPartyId) : undefined);
    if (!pid) { setItems([]); setSel(null); setContracts([]); return; }
    api.get<Program[]>(`/parties/${pid}/programs`)
      .then(r => {
        setItems(r.data);
        if (r.data.length) {
          const toSelect = keepSelId
            ? (r.data.find(p => p.id === keepSelId) ?? r.data[0])
            : r.data[0];
          selectProgram(toSelect);
        } else { setSel(null); setContracts([]); }
      });
  }

  function handlePartyChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const val = e.target.value;
    if (val === "__new__") {
      setShowCreateParty(true);
      setSelectedPartyId("");
      setItems([]); setSel(null); setContracts([]);
      return;
    }
    setSelectedPartyId(val);
    setShowCreateParty(false);
    if (val) loadList(Number(val));
    else { setItems([]); setSel(null); setContracts([]); }
  }

  async function createParty() {
    if (!partyForm.legal_name.trim()) return;
    setPartyBusy(true);
    try {
      const { data } = await api.post<Party>(`/parties`, {
        party_type: partyForm.party_type,
        legal_name: partyForm.legal_name.trim(),
        dba_name: partyForm.dba_name.trim() || undefined,
        scope: "tenant",
      }, { params: { mga } });
      await loadParties();
      setParties(prev => {
        const exists = prev.find(p => p.id === data.id);
        return exists ? prev : [...prev, data];
      });
      setSelectedPartyId(String(data.id));
      setShowCreateParty(false);
      setPartyForm({ legal_name: "", party_type: "carrier", dba_name: "" });
      loadList(data.id);
    } finally { setPartyBusy(false); }

  }

  function selectProgram(p: Program) {
    setSel(p);
    setUploadError(null);
    api.get<Contract[]>(`/programs/${p.id}/contracts`).then(r => {
      setContracts(r.data);
      // Derive the output template the active (or latest) contract is linked to,
      // so a new contract version maps to the same template columns.
      const active = r.data.find(c => c.status === "active") ?? r.data[0];
      setLinkedTemplateId(active?.output_template_id ?? null);
    });
  }

  async function createDraft() {
    if (!creatingName.trim() || !selectedPartyId) return;
    const { data } = await api.post(`/programs`, { name: creatingName, party_id: Number(selectedPartyId) }, { params: { mga } });
    setCreatingName("");
    loadList(); selectProgram(data);
  }

  async function uploadNewVersion(file: File) {
    if (!sel || linkedTemplateId == null) return;
    setBusy(true);
    setUploadError(null);

    try {
      // New contract version mapped to the same output template columns as the
      // current active contract. First-time setup happens on /outputs instead.
      const fd = new FormData();
      fd.append("file", file);
      fd.append("output_template_id", String(linkedTemplateId));
      // When set, the contract is bound to this schedule and only replaces the
      // prior contract for the SAME schedule (many contracts per program).
      if (scheduleKey.trim()) fd.append("schedule_key", scheduleKey.trim());
      const { data } = await api.post<{ id?: number }>(`/programs/${sel.id}/contracts`, fd);

      // Reload contracts so superseded statuses are reflected
      const { data: updatedContracts } = await api.get<Contract[]>(`/programs/${sel.id}/contracts`);
      setContracts(updatedContracts);
      if (fileRef.current) fileRef.current.value = "";
      loadList();
      // Jump straight to the new contract's detail so its output mapping +
      // generated rules are visible immediately.
      if (data?.id) navigate(`/programs/${sel.id}/contracts/${data.id}`);
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      const message = typeof detail === "object" ? detail?.error?.message ?? "Upload failed." : detail ?? "Upload failed.";
      setUploadError(message);
    } finally { setBusy(false); }
  }

  async function activateContract(contractId: number) {
    if (!sel) return;
    setActivatingContract(contractId);
    try {
      const { data } = await api.post<Contract[]>(
        `/programs/${sel.id}/contracts/${contractId}/activate`
      );
      setContracts(data);
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      setUploadError(typeof detail === "string" ? detail : "Could not activate contract.");
    } finally { setActivatingContract(null); }
  }

  async function saveProgram() {
    if (!sel) return;
    setBusy(true);
    try {
      const { data } = await api.put(`/programs/${sel.id}`, {
        name: sel.name, party_id: sel.party_id,
        lead_carrier: sel.lead_carrier, admin_party: sel.admin_party,
        bdx_frequency: sel.bdx_frequency, business_segment: sel.business_segment,
        product_line: sel.product_line, distribution_channel: sel.distribution_channel,
        territory: sel.territory, commercial_terms: sel.commercial_terms,
        status: sel.status,
      });
      setSel(data); loadList(undefined, sel.id);
    } finally { setBusy(false); }
  }

  function patch<K extends keyof Program>(k: K, v: Program[K]) {
    setSel(prev => prev ? { ...prev, [k]: v } : prev);
  }

  const selectedParty = parties.find(p => p.id === Number(selectedPartyId));

  return (
    <>
      {busy && (
        <LoadingOverlay label="Processing the contract — extracting clauses and generating rules. This can take a few minutes…" />
      )}
      <PageHeader title="Programs & Contracts"
        subtitle="Select a party, then manage their programs and upload contracts." />
      <PipelineStepper current="program" />
      <PageBody>
        {/* Party selector */}
        <Card title="Select Party">
          <p className="text-sm text-ink-muted mb-4">
            Programs and contracts are linked to a specific party. Select an existing party or create a new one.
          </p>
          <div className="flex items-end gap-4 flex-wrap">
            <div className="min-w-[280px] max-w-sm flex-1">
            <Field label="Party">
              <Select value={showCreateParty ? "__new__" : selectedPartyId} onChange={handlePartyChange}>
                <option value="">— Select a Party —</option>
                {parties.map(p => (
                  <option key={p.id} value={p.id}>
                    {p.legal_name}{p.dba_name ? ` (${p.dba_name})` : ""} · {p.party_type}
                  </option>
                ))}
                <option value="__new__">➕ Create New Party…</option>
              </Select>
            </Field>
            </div>
            {selectedParty && (
              <div className="mb-1 flex items-center gap-2">
                <span className="pill pill-green">{selectedParty.party_type}</span>
                <span className="text-sm font-medium">{selectedParty.legal_name}</span>
              </div>
            )}
          </div>

          {/* Inline party creation form */}
          {showCreateParty && (
            <div className="mt-4 p-4 border border-border rounded-lg bg-surface-2 space-y-3">
              <div className="font-medium text-sm">Create New Party</div>
              <div className="grid grid-cols-3 gap-3">
                <Field label="Legal name *">
                  <TextInput value={partyForm.legal_name} autoFocus
                    onChange={e => setPartyForm(f => ({ ...f, legal_name: e.target.value }))}
                    placeholder="e.g. Pinnacle Insurance Co." />
                </Field>
                <Field label="Party type *">
                  <Select value={partyForm.party_type}
                    onChange={e => setPartyForm(f => ({ ...f, party_type: e.target.value }))}>
                    {PARTY_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                  </Select>
                </Field>
                {/* <Field label="DBA / Trade name">
                  <TextInput value={partyForm.dba_name}
                    onChange={e => setPartyForm(f => ({ ...f, dba_name: e.target.value }))}
                    placeholder="Optional" />
                </Field> */}
              </div>
              <div className="flex gap-2">
                <Button onClick={createParty} disabled={partyBusy || !partyForm.legal_name.trim()}>
                  <Plus size={14} /> Create Party
                </Button>
                <Button variant="ghost" onClick={() => { setShowCreateParty(false); setPartyForm({ legal_name: "", party_type: "carrier", dba_name: "" }); }}>
                  Cancel
                </Button>
              </div>
            </div>
          )}
        </Card>

        {/* Programs + Contracts (only when party selected) */}
        {selectedParty && !showCreateParty && (
          <div className="grid grid-cols-[260px_1fr] gap-4">
            <Card title="Programs">
              <div className="space-y-1">
                {items.map(p => (
                  <button key={p.id}
                    onClick={() => selectProgram(p)}
                    className={`block w-full text-left px-3 py-2 rounded-md text-sm
                      ${sel?.id === p.id ? "bg-ink text-surface" : "hover:bg-surface-2"}`}>
                    <div className="font-medium truncate">{p.name}</div>
                    <div className={`text-[11px] ${sel?.id === p.id ? "text-white/70" : "text-ink-muted"}`}>
                      {p.lead_carrier ?? "No Carrier"} · {p.status}
                    </div>
                  </button>
                ))}
                {items.length === 0 && (
                  <p className="text-xs text-ink-muted py-2">No programs yet for this party.</p>
                )}
              </div>
              <div className="mt-3 space-y-2">
                <TextInput placeholder="New Program Name…"
                  value={creatingName}
                  onChange={e => setCreatingName(e.target.value)} />
                <Button variant="accent" className="w-full justify-center"
                  onClick={createDraft} disabled={!creatingName.trim()}>
                  <Plus size={14} /> New Program from Contract
                </Button>
              </div>
            </Card>

            {sel ? (
              <div className="space-y-4">
                <Card title="Program Metadata"
                  action={
                    sel.source_contract_file && (
                      <span className="pill pill-blue inline-flex items-center gap-1">
                        <Sparkles size={11} />
                        AI · from {sel.source_contract_file}
                      </span>
                    )
                  }
                >
                  {uploadError && (
                    <div className="mb-3 px-3 py-2 rounded-md bg-red-50 border border-red-200 text-sm text-red-700 flex items-center justify-between">
                      <span>{uploadError}</span>
                      <button onClick={() => setUploadError(null)} className="ml-2 text-red-400 hover:text-red-600">✕</button>
                    </div>
                  )}

                  <div className="grid grid-cols-3 gap-3">
                    <Field label="Program name">
                      <TextInput value={sel.name}
                        onChange={e => patch("name", e.target.value)} />
                    </Field>
                    {/* <Field label="Lead carrier">
                      <TextInput value={sel.lead_carrier ?? ""}
                        onChange={e => patch("lead_carrier", e.target.value)} />
                    </Field>
                    <Field label="Admin party">
                      <TextInput value={sel.admin_party ?? ""}
                        onChange={e => patch("admin_party", e.target.value)} />
                    </Field> */}
                    <Field label="BDX frequency">
                      <Select value={sel.bdx_frequency ?? ""}
                        onChange={e => patch("bdx_frequency", e.target.value)}>
                        <option value="">—</option>
                        {BDX_FREQUENCIES.map(([v, l]) =>
                          <option key={v} value={v}>{l}</option>)}
                      </Select>
                    </Field>
                    {/* <Field label="Business segment">
                      <TextInput value={sel.business_segment ?? ""}
                        onChange={e => patch("business_segment", e.target.value)} />
                    </Field>
                    <Field label="Product line">
                      <TextInput value={sel.product_line ?? ""}
                        onChange={e => patch("product_line", e.target.value)} />
                    </Field>
                    <Field label="Distribution channel">
                      <TextInput value={sel.distribution_channel ?? ""}
                        onChange={e => patch("distribution_channel", e.target.value)} />
                    </Field>
                    <Field label="Territory">
                      <TextInput value={sel.territory ?? ""}
                        onChange={e => patch("territory", e.target.value)} />
                    </Field> */}
                    <Field label="Status">
                      <Select value={sel.status ?? "draft"}
                        onChange={e => patch("status", e.target.value)}>
                        <option value="draft">Draft</option>
                        <option value="active">Active</option>
                      </Select>
                    </Field>
                  </div>
                  {/* Sticky confirm bar — always visible at the card's bottom */}
                  <div className="sticky bottom-0 -mx-5 -mb-5 mt-5 px-5 py-3 bg-white/95
                    backdrop-blur border-t border-border rounded-b-lg
                    flex items-center justify-end gap-3">
                    <span className="text-xs text-ink-muted mr-auto">
                      Review the details, then confirm to save the program.
                    </span>
                    <Button onClick={saveProgram} disabled={busy}>
                      Confirm Program
                    </Button>
                  </div>
                </Card>

                <Card title="Contracts"
                  action={
                    linkedTemplateId != null ? (
                      <>
                        <input
                          value={scheduleKey}
                          onChange={e => setScheduleKey(e.target.value)}
                          placeholder="Schedule (e.g. Schedule A)"
                          title="Bind this contract to a schedule so one program can hold many contracts. Leave blank for a single contract."
                          className="text-xs px-2 py-1.5 rounded-md border border-border mr-2 w-48" />
                        <Button
                          variant="ghost"
                          onClick={() => fileRef.current?.click()}
                          disabled={busy}>
                          <Upload size={14} /> Upload Contract
                        </Button>
                        <input ref={fileRef} type="file" hidden accept=".pdf,.doc,.docx,.txt"
                          onChange={e => e.target.files?.[0] && uploadNewVersion(e.target.files[0])} />
                      </>
                    ) : (
                      <span className="text-xs text-ink-muted">
                        Only the <span className="font-medium text-emerald-700">active</span> contract
                        is used for validation and output generation.
                      </span>
                    )
                  }>
                  {uploadError && (
                    <div className="mb-3 px-3 py-2 rounded-md bg-red-50 border border-red-200 text-sm text-red-700 flex items-start justify-between gap-2">
                      <span><span className="font-medium">Upload failed.</span> {uploadError}</span>
                      <button onClick={() => setUploadError(null)} className="text-red-400 hover:text-red-600 shrink-0">✕</button>
                    </div>
                  )}
                  {linkedTemplateId != null && (
                    <p className="text-xs text-ink-muted mb-3">
                      Upload a new version to replace the active contract — it maps to the same
                      output template columns. Previous versions are kept as history (superseded)
                      and can be re-activated if needed.
                    </p>
                  )}
                  {contracts.length === 0 ? (
                    <div className="py-6 text-center space-y-2">
                      <p className="text-sm text-ink-muted">
                        No output template or contract set up for this program yet.
                      </p>
                      <Link to="/outputs/new-template"
                        className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-md bg-accent
                          text-white text-sm font-medium hover:opacity-90 no-underline">
                        Set Up Output Template + Contract <ArrowRight size={14} />
                      </Link>
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {contracts.map(c => {
                        const isActive = c.status === CONTRACT_ACTIVE;
                        const isSuperseded = c.status === CONTRACT_SUPERSEDED;
                        const canActivate = !isActive && c.status !== "failed" && c.status !== "drafted" && c.status !== "extracting";
                        const activating = activatingContract === c.id;
                        return (
                          <div key={c.id}
                            className={`rounded-lg border px-4 py-3 flex items-start gap-3 transition
                              ${isActive
                                ? "border-emerald-300 bg-emerald-50"
                                : isSuperseded
                                  ? "border-border bg-surface-2 opacity-70"
                                  : "border-border bg-white"}`}>
                            {/* status icon */}
                            <div className="mt-0.5 shrink-0">
                              {isActive
                                ? <CheckCircle2 size={16} className="text-emerald-600" />
                                : <div className="w-4 h-4 rounded-full border-2 border-ink-soft/40" />}
                            </div>

                            {/* content */}
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <Link
                                  to={`/programs/${sel.id}/contracts/${c.id}`}
                                  className={`font-medium text-sm truncate ${isSuperseded ? "text-ink-muted" : ""}`}>
                                  {c.filename ?? "—"}
                                </Link>
                                {c.schedule_key && (
                                  <span className="text-[11px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200">
                                    {c.schedule_key}
                                  </span>
                                )}
                                {isActive && (
                                  <span className="pill pill-green text-[11px]">Active</span>
                                )}
                                {isSuperseded && (
                                  <span className="pill pill-grey text-[11px]">Superseded</span>
                                )}
                                {c.status === "failed" && (
                                  <span className="pill pill-red text-[11px]">Failed</span>
                                )}
                                {(c.status === "drafted" || c.status === "extracting") && (
                                  <span className="pill pill-amber text-[11px] inline-flex items-center gap-1">
                                    <Loader2 size={10} className="animate-spin" /> Extracting…
                                  </span>
                                )}
                              </div>
                              <div className="text-[11px] text-ink-muted mt-0.5">
                                Uploaded {fmtStamp(c.created_at)}
                              </div>
                            </div>

                            {/* actions */}
                            <div className="shrink-0 flex items-center gap-2">
                              {canActivate && (
                                <button
                                  onClick={() => activateContract(c.id)}
                                  disabled={activating || !!activatingContract}
                                  className="text-xs underline text-accent hover:no-underline disabled:opacity-50">
                                  Set as Active
                                </button>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </Card>
              </div>
            ) : (
              <Card>
                <p className="text-sm text-ink-muted">
                  Select or create a program on the left to begin.
                </p>
              </Card>
            )}
          </div>
        )}

        {!selectedParty && !showCreateParty && (
          <Card>
            <p className="text-sm text-ink-muted text-center py-6">
              Select or create a party above to manage their programs and contracts.
            </p>
          </Card>
        )}

      </PageBody>
    </>
  );
}
