import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { Provider } from "react-redux";
import { PersistGate } from "redux-persist/integration/react";
import { store, persistor } from "./store";
import App from "./App";
import { loadFeatures } from "./features";
import "./index.css";
import "./proto.css";

// NOTE: React.StrictMode intentionally removed. In dev it double-mounts every
// component and double-fires every effect, which shows every API call twice in
// the network tab and kept being mistaken for a real bug. Re-add it if you
// want its dev-only lifecycle checks back.
// The Phase 2 switch is read before the first draw, so no route or sidebar
// entry appears and then vanishes (features.ts).
loadFeatures().finally(() => ReactDOM.createRoot(document.getElementById("root")!).render(
  <Provider store={store}>
    {/* Hold rendering until the persisted auth state is rehydrated, so the
        first components to mount already see the stored tokens. */}
    <PersistGate loading={null} persistor={persistor}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </PersistGate>
  </Provider>
));
