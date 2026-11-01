import React from 'react';
import { CheckCircle, AlertCircle, Clock, ArrowRight, CornerDownRight, Eye } from 'lucide-react';
import { AgentStep } from '../lib/types';
import { Lang, translations } from '../lib/i18n';

interface StepTimelineProps {
  steps: AgentStep[];
  lang: Lang;
}

export const StepTimeline: React.FC<StepTimelineProps> = ({ steps, lang }) => {
  const t = translations[lang];

  if (steps.length === 0) {
    return (
      <div className="bg-white border border-slate-200 rounded-xl p-6 text-center text-slate-500 text-xs">
        <Clock className="w-6 h-6 mx-auto mb-2 text-slate-400" />
        <p>No actions yet. Start application to see live progress.</p>
      </div>
    );
  }

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm space-y-3">
      <h3 className="font-semibold text-slate-900 text-sm flex items-center gap-2">
        <Clock className="w-4 h-4 text-sky-600" />
        {t.stepTimeline} ({steps.length})
      </h3>

      <div className="space-y-3 max-h-80 overflow-y-auto pr-1">
        {steps.map((st, idx) => {
          const isOk = st.status === 'ok';
          const isError = st.status === 'error';
          const isWaiting = st.status === 'waiting_human';

          return (
            <div key={idx} className="flex items-start gap-2.5 text-xs border-b border-slate-100 pb-2.5 last:border-b-0">
              <div className="mt-0.5">
                {isOk && <CheckCircle className="w-4 h-4 text-emerald-500" />}
                {isError && <AlertCircle className="w-4 h-4 text-rose-500" />}
                {isWaiting && <Clock className="w-4 h-4 text-amber-500 animate-pulse" />}
              </div>

              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-slate-800 capitalize">
                    {st.action.replace('_', ' ')}
                  </span>
                  <span className="text-[10px] text-slate-400 font-mono">
                    Step {st.step_no}
                  </span>
                </div>

                {st.summary && <p className="text-slate-600 text-xs mt-0.5">{st.summary}</p>}
                {st.rationale && (
                  <p className="text-slate-500 text-[11px] italic mt-0.5 line-clamp-2">
                    "{st.rationale}"
                  </p>
                )}
                {st.error && <p className="text-rose-600 text-[11px] mt-0.5 font-medium">{st.error}</p>}
              </div>

              {st.thumbnail_b64 && (
                <div className="flex-shrink-0 w-12 h-9 rounded border border-slate-200 overflow-hidden bg-slate-50">
                  <img
                    src={`data:image/jpeg;base64,${st.thumbnail_b64}`}
                    alt="Step thumbnail"
                    className="w-full h-full object-cover"
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

