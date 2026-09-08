import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import Layout from "./components/Layout";
import GlobalErrorPopup from "./components/GlobalErrorPopup";
import Login from "./pages/Login";
import ResetPassword from "./pages/ResetPassword";
import Home from "./pages/Home";
import Tenant from "./pages/Tenant";
import Tenants from "./pages/Tenants";
import AddTenant from "./pages/AddTenant";
import AdminUsers from "./pages/AdminUsers";
import BrokerDashboard from "./pages/BrokerDashboard";
import OperatorHome from "./pages/OperatorHome";
import BrokerContracts from "./pages/BrokerContracts";
import BrokerBordereau from "./pages/BrokerBordereau";
import BrokerUsers from "./pages/BrokerUsers";
import TenantDetail from "./pages/TenantDetail";
import Parties from "./pages/Parties";
import AddParty from "./pages/AddParty";
import PartyDetail from "./pages/PartyDetail";
import Programs from "./pages/Programs";
import ProgramManagement from "./pages/ProgramManagement";
import AddProgram from "./pages/AddProgram";
import ProgramBrokers from "./pages/ProgramBrokers";
import ContractDetail from "./pages/ContractDetail";
import ContractRecord from "./pages/ContractRecord";
import ContractSignature from "./pages/ContractSignature";
import Contracts from "./pages/Contracts";
import ContractNew from "./pages/ContractNew";
import ContractUpload from "./pages/ContractUpload";
import BrokerContractNew from "./pages/BrokerContractNew";
import Approvals from "./pages/Approvals";
// Create-a-Contract, step 4 from the carrier's side: what is out for
// signature and where each one got to. Steps 1-3 are the wizard at
// /contracts/new.
import ContractSignatures from "./pages/ContractSignatures";
// The signing screen itself. PUBLIC — reached from an emailed link by
// people who have no account here, so it sits outside RequireAuth.
import SignContract from "./pages/SignContract";
import Mapping from "./pages/Mapping";
import DirectRun from "./pages/DirectRun";
import DirectSetup from "./pages/DirectSetup";
import BordereauSetups from "./pages/BordereauSetups";
import BordereauSetupDetail from "./pages/BordereauSetupDetail";
import BordereauSetupEdit from "./pages/BordereauSetupEdit";
import AdminMappingTasks from "./pages/AdminMappingTasks";
import KavachioAdminDashboard from "./pages/KavachioAdminDashboard";
import Outputs from "./pages/Outputs";
import OutputTemplate from "./pages/OutputTemplate";
import UploadExceptions from "./pages/UploadExceptions";
import RuleReview from "./pages/RuleReview";
import Users from "./pages/Users";
import RuleLibrary from "./pages/RuleLibrary";
import RuleForm from "./pages/RuleForm";
import RecentRuns from "./pages/RecentRuns";
import Calendar from "./pages/Calendar";
import BordereauCalendar from "./pages/BordereauCalendar";
// Feature 10 — file intake channels.
import FilesArrive from "./pages/FilesArrive";
import FilesReceived from "./pages/FilesReceived";
import AddUser from "./pages/AddUser";
import Profile from "./pages/Profile";
import Welcome from "./pages/Welcome";
import { useEffect } from "react";
import {
  AUTH_EVENT, armAutoLogout, clearAuth, getUser, isRefreshTokenExpired,
} from "./auth";
import { canAccessPath, landingPath } from "./access";
import BrokerDetail from "./pages/BrokerDetail";
import Brokers from "./pages/Brokers";

function RequireAuth({ children }: { children: JSX.Element }) {
  // A stored user with an expired refresh token is a dead session — treat it
  // as signed out immediately instead of waiting for the first 401.
  if (isRefreshTokenExpired()) clearAuth();
  if (!getUser()) return <Navigate to="/login" replace />;
  return children;
}

// Role guard for the CURRENT url, driven by the ROUTE_ACCESS map in access.ts.
// Wrapped around the whole authenticated tree, so hiding a sidebar item is no
// longer the only thing standing between a role and a screen it may not use:
// typing the URL (or following a stale bookmark, e.g. /admin/mapping-tasks as a
// tenant_admin) lands on the role's own dashboard instead of mounting a screen
// whose every request the backend would 403.
function RequireAccess({ children }: { children: JSX.Element }) {
  const { pathname } = useLocation();
  if (!canAccessPath(pathname)) return <Navigate to={landingPath()} replace />;
  return children;
}

// Role-based landing: Kavachio platform admins get their own Dashboard,
// everyone else the tenant Home dashboard.
function DefaultHome() {
  return <Navigate to={landingPath()} replace />;
}

export default function App() {
  // Log out automatically the moment the refresh token expires — even if the
  // tab sits idle. Re-armed on login/logout (AUTH_EVENT for this tab, the
  // native `storage` event for other tabs).
  useEffect(() => {
    armAutoLogout();
    const rearm = () => armAutoLogout();
    window.addEventListener(AUTH_EVENT, rearm);
    window.addEventListener("storage", rearm);
    return () => {
      window.removeEventListener(AUTH_EVENT, rearm);
      window.removeEventListener("storage", rearm);
    };
  }, []);

  return (
    <>
    {/* Friendly popup for unexpected failures (5xx / server unreachable) on ANY
        screen, login included — see api/client.ts + GlobalErrorPopup. */}
    <GlobalErrorPopup />
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/reset" element={<ResetPassword />} />
      {/* Signing a contract from an emailed link. No Layout, no session,
          no guard: the token in the URL is the whole credential and it
          stands for exactly one signer on exactly one contract. The
          insurer's signer and the broker's both land here. */}
      <Route path="/sign" element={<SignContract />} />
      {/* /welcome is the tenant-admin first-run wizard — it has no Layout shell,
          so it carries the guards itself. */}
      <Route path="/welcome" element={<RequireAuth><RequireAccess><Welcome /></RequireAccess></RequireAuth>} />
      <Route element={<RequireAuth><RequireAccess><Layout /></RequireAccess></RequireAuth>}>
        <Route index element={<DefaultHome />} />
        <Route path="/home" element={<Home />} />
        <Route path="/admin/dashboard" element={<KavachioAdminDashboard />} />
        <Route path="/tenant" element={<Tenant />} />      {/* Organization (this tenant's own settings) */}
        {/* Broker seats. Everything here is READ of what a carrier gave them. */}
        <Route path="/broker" element={<BrokerDashboard />} />
        <Route path="/operator" element={<OperatorHome />} />   {/* the broker's day-to-day seat */}
        <Route path="/broker/contracts" element={<BrokerContracts />} />
        {/* Step A of the flow: the broker brings a contract and waits. */}
        <Route path="/broker/contracts/new" element={<BrokerContractNew />} />
        {/* The broker's monthly run. Both broker seats reach it: an admin
            submits, and an operator is the seat added to do exactly this. */}
        <Route path="/broker/bordereau" element={<BrokerBordereau />} />
        <Route path="/broker/users" element={<BrokerUsers />} />   {/* The broker staffs itself */}
        <Route path="/admin/users" element={<AdminUsers />} />   {/* Everyone on the platform, read-only */}
        <Route path="/tenants" element={<Tenants />} />    {/* Tenants directory (Kavachio platform admin) */}
        <Route path="/tenants/new" element={<AddTenant />} />
        <Route path="/tenants/:mga" element={<TenantDetail />} />
        <Route path="/parties" element={<Parties />} />
        <Route path="/parties/new" element={<AddParty />} />
        <Route path="/parties/:id" element={<PartyDetail />} />
        <Route path="/programs" element={<Programs />} />
        {/* The carrier hierarchy: brokers are reached from the carrier, not
            from a tenant — the same broker produces for several carriers. */}
        <Route path="/brokers" element={<Brokers />} />
        <Route path="/brokers/:brokerId" element={<BrokerDetail />} />
        {/* Carrier-scoped oversight dashboard. Without ?carrier= it renders its
            own carrier picker, so the route needs no param of its own. */}
        <Route path="/program-management" element={<ProgramManagement />} />
        <Route path="/programs/new" element={<AddProgram />} />
        <Route path="/programs/:programId/brokers" element={<ProgramBrokers />} />   {/* the mesh, managed */}
        {/* The contract as a RECORD — terms, documents, lifecycle. The
            programme-scoped route below is what it PRODUCED: clauses and rules. */}
        <Route path="/contracts" element={<Contracts />} />
        <Route path="/contracts/new" element={<ContractNew />} />
        {/* The other way in: a wording that already exists, read on upload. */}
        <Route path="/contracts/upload" element={<ContractUpload />} />
        <Route path="/contracts/:contractId" element={<ContractRecord />} />
        {/* Screen only — no signing provider is connected. See the page. */}
        <Route path="/contracts/:contractId/signature" element={<ContractSignature />} />
        <Route path="/approvals" element={<Approvals />} />   {/* the gate */}
        <Route path="/programs/:programId/contracts/:contractId" element={<ContractDetail />} />
        {/* Watching the signing rounds. A round is STARTED from the
            contract itself, once both sides have agreed the terms — see
            esign_routes' in-app door — not from a screen of its own. */}
        <Route path="/contracts/signatures" element={<ContractSignatures />} />
        <Route path="/direct" element={<DirectRun />} />
        <Route path="/direct/setup" element={<DirectSetup />} />
        <Route path="/direct/setups" element={<BordereauSetups />} />
        <Route path="/direct/setups/:id" element={<BordereauSetupDetail />} />
        <Route path="/direct/setups/:id/edit" element={<BordereauSetupEdit />} />
        <Route path="/admin/mapping-tasks" element={<AdminMappingTasks />} />
        <Route path="/uploads/:uploadId/exceptions" element={<UploadExceptions />} />
        <Route path="/uploads/:uploadId/exceptions/rule/:ruleId" element={<RuleReview />} />
        <Route path="/uploads/mapper/:mapperId" element={<Mapping />} />
        <Route path="/outputs" element={<Outputs />} />
        <Route path="/outputs/new-template" element={<Outputs />} />
        <Route path="/outputs/generate" element={<Outputs />} />
        <Route path="/outputs/templates/:id" element={<OutputTemplate />} />
        <Route path="/users" element={<Users />} />
        {/* Rule library — managed BDX rules. tenant_admin sees their own tenant's
            rules; kavachio_admin sees the platform-wide (global) rules. The
            backend scopes the rows by role; ROUTE_ACCESS lets both in. */}
        <Route path="/rule-library" element={<RuleLibrary />} />
        <Route path="/rule-library/new" element={<RuleForm />} />
        <Route path="/rule-library/:id/edit" element={<RuleForm />} />
        <Route path="/runs" element={<RecentRuns />} />
        <Route path="/calendar" element={<Calendar />} />
        {/* The carrier's view of the same data: every broker's obligation for a
            due month, rather than one programme's deadlines. /calendar stays as
            the place a single programme's schedule is SET. */}
        <Route path="/bordereau-calendar" element={<BordereauCalendar />} />
        {/* How a broker's file reaches you, and everything that has landed. */}
        <Route path="/intake" element={<FilesArrive />} />
        <Route path="/intake/arrivals" element={<FilesReceived />} />
        <Route path="/users/new" element={<AddUser />} />
        <Route path="/profile" element={<Profile />} />
      </Route>
      <Route path="*" element={<DefaultHome />} />
    </Routes>
    </>
  );
}
