"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui";

/**
 * Re-authentication at the moment of a consequential act.
 *
 * Being signed in is not evidence that the head teacher is at the keyboard. A
 * session left open in a staff room must not be able to publish a term's
 * results, so release exchanges a current authenticator code for a single-use
 * grant that expires in five minutes.
 *
 * The prompt says why it is asking. A security control that appears without
 * explanation is one people learn to click through.
 */
export function StepUp({
  action,
  onGranted,
  onCancel,
}: {
  action: string;
  onGranted: (token: string) => Promise<void> | void;
  onCancel: () => void;
}) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");

    try {
      const { step_up_token } = await api.post<{ step_up_token: string }>(
        "/auth/step-up",
        { code: code.trim() },
      );
      await onGranted(step_up_token);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 rounded-md border border-provisional/30 bg-provisional-soft px-4 py-3.5"
    >
      <p className="text-sm font-medium text-provisional">
        Confirm it is you
      </p>
      <p className="mt-1 text-xs text-slate">
        {action} is published to students and parents and cannot be quietly
        undone. Enter the current code from your authenticator app.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label htmlFor="step-up-code" className="sr-only">
          Authenticator code
        </label>
        <input
          id="step-up-code"
          value={code}
          onChange={(event) => setCode(event.target.value)}
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={10}
          autoFocus
          placeholder="000000"
          className="measured w-28 rounded-md border border-rule bg-card px-3 py-1.5 text-sm tracking-widest outline-none focus:border-signal"
        />
        <Button type="submit" disabled={busy || code.trim().length < 6}>
          {busy ? "Confirming…" : "Confirm and release"}
        </Button>
        <Button type="button" variant="quiet" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>

      {error && (
        <p role="alert" className="mt-2 text-xs text-rejected">
          {error}
        </p>
      )}
    </form>
  );
}
