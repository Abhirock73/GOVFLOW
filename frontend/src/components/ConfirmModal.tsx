import React from 'react';
import { ShieldCheck, AlertCircle, Check, X } from 'lucide-react';
import { Lang, translations } from '../lib/i18n';

interface ConfirmModalProps {
  isOpen: boolean;
  summary: Record<string, string>;
  lang: Lang;
  onConfirm: () => void;
  onCancel: () => void;
  isSubmitting?: boolean;
}

export const ConfirmModal: React.FC<ConfirmModalProps> = ({
  isOpen,
  summary,
  lang,
  onConfirm,
  onCancel,
  isSubmitting = false,
}) => {
  const t = translations[lang];

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl max-w-lg w-full p-6 shadow-2xl border border-slate-200 space-y-4 animate-in fade-in zoom-in-95 duration-150">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-amber-100 flex items-center justify-center text-amber-600">
            <ShieldCheck className="w-6 h-6" />
          </div>
          <div>
            <h3 className="font-bold text-slate-900 text-base">{t.confirmSubmitPrompt}</h3>
            <p className="text-xs text-slate-500">{t.confirmSubmitText}</p>
          </div>
        </div>

        <div className="bg-slate-50 rounded-xl p-4 border border-slate-200 space-y-2 max-h-60 overflow-y-auto">
          <h4 className="text-xs font-semibold text-slate-700 uppercase tracking-wider">
            Review Submitted Details (Masked)
          </h4>
          <div className="space-y-1.5 text-xs">
            {Object.entries(summary).map(([key, val]) => (
              <div key={key} className="flex justify-between items-center py-1 border-b border-slate-200/60 last:border-0">
                <span className="text-slate-500 capitalize">{key.replace('_', ' ')}:</span>
                <span className="font-mono font-medium text-slate-900 text-right">{val}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="p-3 bg-amber-50 rounded-lg border border-amber-200 text-xs text-amber-800 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 flex-shrink-0 text-amber-600 mt-0.5" />
          <span>
            This action will submit your application to the official government portal. Ensure that all details are accurate.
          </span>
        </div>

        <div className="flex items-center justify-end gap-3 pt-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={isSubmitting}
            className="px-4 py-2 border border-slate-300 hover:bg-slate-50 text-slate-700 rounded-lg text-xs font-medium flex items-center gap-1.5 transition cursor-pointer disabled:opacity-50"
          >
            <X className="w-4 h-4" />
            <span>{t.confirmNo}</span>
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isSubmitting}
            className={`px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-xs font-semibold flex items-center gap-1.5 shadow-sm transition ${
              isSubmitting ? 'opacity-75 cursor-not-allowed' : 'cursor-pointer'
            }`}
          >
            {isSubmitting ? (
              <>
                <span className="w-3.5 h-3.5 border-2 border-white/40 border-t-white rounded-full animate-spin inline-block" />
                <span>Submitting...</span>
              </>
            ) : (
              <>
                <Check className="w-4 h-4" />
                <span>{t.confirmYes}</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};

