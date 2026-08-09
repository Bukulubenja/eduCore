"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";

/**
 * Reads the rotating code shown on the staff-room display device.
 *
 * The display mints a fresh, server-signed, 30-second token from
 * `GET /attendance/qr-token` (see `educore/presence/qr.py`) and shows it as a
 * QR code; this component is the phone-side camera that reads it back. That
 * token becomes `signals.qr.token` on the next check-in/check-out, and it is
 * the single heaviest-weighted signal the evaluator scores (35 of a possible
 * 110 points -- see `default_weights` in `educore/presence/models.py`).
 *
 * No new dependency: `BarcodeDetector` is a built-in browser API (Chromium
 * and Safari 17+) and is used directly rather than pulling in a JS QR
 * decoder. Where it is unsupported -- or the camera is denied -- a manual
 * "enter the code" field covers the same ground; the display screen can show
 * the raw token as text alongside its QR rendering for exactly this case.
 */

type Props = {
  onCapture: (token: string) => void;
};

type BarcodeDetectorLike = {
  detect: (source: CanvasImageSource) => Promise<{ rawValue: string }[]>;
};

declare global {
  interface Window {
    BarcodeDetector?: new (options: { formats: string[] }) => BarcodeDetectorLike;
  }
}

export function QrCapture({ onCapture }: Props) {
  const [mode, setMode] = useState<"idle" | "scanning" | "manual" | "error">(
    "idle",
  );
  const [manualToken, setManualToken] = useState("");
  const [error, setError] = useState("");
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const stopRef = useRef(false);

  const supported =
    typeof window !== "undefined" && "BarcodeDetector" in window;

  useEffect(() => {
    return () => {
      stopRef.current = true;
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  async function startScan() {
    if (!supported || !window.BarcodeDetector) {
      setMode("manual");
      return;
    }

    setError("");
    setMode("scanning");
    stopRef.current = false;

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play();
      }

      const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
      const loop = async () => {
        if (stopRef.current || !videoRef.current) return;
        try {
          const codes = await detector.detect(videoRef.current);
          if (codes.length > 0) {
            stopScan();
            onCapture(codes[0].rawValue);
            return;
          }
        } catch {
          // A frame with nothing decodable is normal; keep scanning.
        }
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    } catch {
      setError(
        "Camera access was denied or unavailable. Enter the code shown on the display instead.",
      );
      setMode("manual");
    }
  }

  function stopScan() {
    stopRef.current = true;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setMode("idle");
  }

  function submitManual() {
    if (!manualToken.trim()) return;
    onCapture(manualToken.trim());
    setManualToken("");
    setMode("idle");
  }

  if (mode === "scanning") {
    return (
      <div className="space-y-3">
        <div className="overflow-hidden rounded-lg border border-rule bg-ink">
          <video
            ref={videoRef}
            className="aspect-square w-full object-cover"
            playsInline
            muted
          />
        </div>
        <p className="text-center text-xs text-slate">
          Point your camera at the code on the staff-room display.
        </p>
        <Button variant="quiet" onClick={stopScan} className="w-full">
          Cancel
        </Button>
      </div>
    );
  }

  if (mode === "manual") {
    return (
      <div className="space-y-2">
        {error && (
          <p role="alert" className="text-xs text-rejected">
            {error}
          </p>
        )}
        <label htmlFor="qr-manual" className="text-xs font-medium text-slate">
          Code from the display
        </label>
        <input
          id="qr-manual"
          value={manualToken}
          onChange={(event) => setManualToken(event.target.value)}
          className="w-full rounded-md border border-rule bg-card px-3 py-2 text-sm outline-none focus:border-signal"
          placeholder="Paste or type the code"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
        />
        <div className="flex gap-2">
          <Button onClick={submitManual} className="flex-1">
            Use this code
          </Button>
          <Button variant="quiet" onClick={() => setMode("idle")}>
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <Button variant="quiet" onClick={startScan} className="flex-1">
        Scan display code
      </Button>
      <Button variant="quiet" onClick={() => setMode("manual")}>
        Enter manually
      </Button>
    </div>
  );
}
