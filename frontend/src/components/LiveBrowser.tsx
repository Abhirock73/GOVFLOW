import React, { useState, useRef, useEffect } from 'react';
import { Monitor, MousePointer, ShieldAlert, CheckCircle, RefreshCw, KeyRound } from 'lucide-react';
import { Lang, translations } from '../lib/i18n';
import { GovFlowWebSocket } from '../lib/ws';
import { HumanRequest } from '../lib/types';

interface LiveBrowserProps {
  wsClient: GovFlowWebSocket | null;
  pendingRequest: HumanRequest | null;
  lang: Lang;
  onHumanReply: (reply: { text?: string; approved?: boolean; captcha_solved?: boolean }) => void;
}

export const LiveBrowser: React.FC<LiveBrowserProps> = ({
  wsClient,
  pendingRequest,
  lang,
  onHumanReply,
}) => {
  const t = translations[lang];
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [isTakeover, setIsTakeover] = useState<boolean>(false);
  const [otpValue, setOtpValue] = useState<string>('');
  const canvasRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!wsClient) return;

    const unsubscribe = wsClient.subscribe((data) => {
      if (data.type === 'screencast_frame' && data.data_b64) {
        setFrameUrl(`data:image/jpeg;base64,${data.data_b64}`);
      } else if (data.type === 'takeover_status') {
        setIsTakeover(!!data.active);
      }
    });

    return () => {
      unsubscribe();
    };
  }, [wsClient]);

  const toggleTakeover = () => {
    if (!wsClient) return;
    if (isTakeover) {
      wsClient.sendTakeoverEnd();
      setIsTakeover(false);
      if (pendingRequest?.kind === 'captcha') {
        onHumanReply({ captcha_solved: true });
      }
    } else {
      wsClient.sendTakeoverBegin('Citizen takeover');
      setIsTakeover(true);
    }
  };

  const handleMouseEvent = (action: string, e: React.MouseEvent) => {
    if (!isTakeover || !wsClient || !canvasRef.current) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    wsClient.sendTakeoverInput({
      kind: 'mouse',
      action,
      x,
      y,
      canvas_width: rect.width,
      canvas_height: rect.height,
      button: e.button === 2 ? 'right' : 'left',
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!isTakeover || !wsClient) return;
    wsClient.sendTakeoverInput({
      kind: 'key',
      action: 'press',
      key: e.key,
      shift: e.shiftKey,
      ctrl: e.ctrlKey,
      alt: e.altKey,
    });
  };

  return (
    <div className="bg-slate-900 rounded-xl overflow-hidden shadow-lg border border-slate-800 flex flex-col h-full">
      {/* Top control bar */}
      <div className="bg-slate-800 px-4 py-2.5 flex items-center justify-between border-b border-slate-700">
        <div className="flex items-center gap-2">
          <Monitor className="w-4 h-4 text-sky-400" />
          <span className="text-xs font-semibold text-slate-200">Live Portal Browser View</span>
          <span className="flex h-2 w-2 relative">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
          </span>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={toggleTakeover}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 transition ${
              isTakeover
                ? 'bg-amber-500 hover:bg-amber-600 text-white animate-pulse'
                : 'bg-slate-700 hover:bg-slate-600 text-slate-200'
            }`}
          >
            <MousePointer className="w-3.5 h-3.5" />
            <span>{isTakeover ? t.releaseControl : t.takeOver}</span>
          </button>
        </div>
      </div>

      {/* Human Intervention Banner */}
      {pendingRequest && (
        <div className="bg-amber-500/90 text-slate-950 px-4 py-2.5 flex items-center justify-between gap-3 backdrop-blur border-b border-amber-600">
          <div className="flex items-center gap-2 text-xs font-medium">
            <ShieldAlert className="w-4 h-4 text-slate-950 flex-shrink-0" />
            <span>{pendingRequest.question}</span>
          </div>

          {pendingRequest.kind === 'otp' && (
            <div className="flex items-center gap-2">
              <input
                type="text"
                placeholder="Enter OTP"
                value={otpValue}
                onChange={(e) => setOtpValue(e.target.value)}
                className="px-2 py-1 bg-white text-slate-900 rounded text-xs w-28 font-mono border"
              />
              <button
                onClick={() => {
                  onHumanReply({ text: otpValue });
                  setOtpValue('');
                }}
                className="px-2.5 py-1 bg-slate-900 hover:bg-slate-800 text-white rounded text-xs font-medium"
              >
                Submit OTP
              </button>
            </div>
          )}

          {pendingRequest.kind === 'captcha' && !isTakeover && (
            <button
              onClick={toggleTakeover}
              className="px-3 py-1 bg-slate-900 text-white rounded text-xs font-medium flex-shrink-0"
            >
              Click to Solve
            </button>
          )}

          {pendingRequest.kind === 'captcha' && isTakeover && (
            <button
              onClick={toggleTakeover}
              className="px-3 py-1 bg-emerald-700 hover:bg-emerald-800 text-white rounded text-xs font-medium flex-shrink-0"
            >
              Solved, Resume
            </button>
          )}
        </div>
      )}

      {/* Viewport Canvas / Frame */}
      <div
        ref={canvasRef}
        tabIndex={0}
        onKeyDown={handleKeyDown}
        onClick={(e) => handleMouseEvent('click', e)}
        onMouseDown={(e) => handleMouseEvent('down', e)}
        onMouseUp={(e) => handleMouseEvent('up', e)}
        onMouseMove={(e) => isTakeover && handleMouseEvent('move', e)}
        className={`flex-1 relative overflow-hidden flex items-center justify-center bg-slate-950 outline-none ${
          isTakeover ? 'cursor-crosshair ring-2 ring-amber-500' : ''
        }`}
      >
        {frameUrl ? (
          <img
            src={frameUrl}
            alt="Live browser stream"
            className="w-full h-full object-contain select-none pointer-events-none"
          />
        ) : wsClient ? (
          <div className="text-center text-slate-500 text-xs p-6 space-y-2">
            <RefreshCw className="w-8 h-8 mx-auto animate-spin text-slate-600" />
            <p>Launching browser and waiting for live stream...</p>
          </div>
        ) : (
          <div className="text-center text-slate-500 text-xs p-6 space-y-3 max-w-xs">
            <Monitor className="w-10 h-10 mx-auto text-slate-600 stroke-[1.5]" />
            <div className="space-y-1">
              <p className="font-medium text-slate-300">Live Browser Session</p>
              <p className="text-[11px] text-slate-500">
                Click <strong className="text-sky-400">"Start Application"</strong> above to discover the portal and begin autonomous form filling.
              </p>
            </div>
          </div>
        )}

        {isTakeover && (
          <div className="absolute top-3 left-3 bg-amber-500 text-slate-950 text-[11px] font-bold px-2 py-1 rounded shadow">
            MANUAL TAKEOVER ACTIVE
          </div>
        )}
      </div>
    </div>
  );
};

