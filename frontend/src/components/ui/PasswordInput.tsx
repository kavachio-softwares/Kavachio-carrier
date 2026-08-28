import { InputHTMLAttributes, useState } from "react";
import { Eye, EyeOff } from "lucide-react";

/** A password field with a show/hide (eye) toggle.
 *
 *  Every prop is forwarded to the underlying <input>, so this drops into either
 *  design system unchanged — the caller keeps its own className/style and this
 *  only adds the relative wrapper needed to position the toggle plus enough
 *  right padding that the text never runs under the icon.
 *
 *  `type` is owned here (password ⇄ text); the button is type="button" so it can
 *  never submit the form it sits in.
 */
export function PasswordInput({ style, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  const [show, setShow] = useState(false);
  const label = show ? "Hide password" : "Show password";
  return (
    <div style={{ position: "relative" }}>
      <input
        {...rest}
        type={show ? "text" : "password"}
        // Spread the caller's style first so this padding wins over any shorthand.
        style={{ ...style, paddingRight: 38 }}
      />
      <button
        type="button"
        onClick={() => setShow(s => !s)}
        aria-label={label}
        title={label}
        style={{
          position: "absolute", top: 0, bottom: 0, right: 8,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "transparent", border: 0, padding: 4, margin: 0,
          cursor: "pointer", color: "#8B93A2", lineHeight: 0,
        }}
      >
        {show ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  );
}

export default PasswordInput;
