import React from 'react';
import { CheckCircle2, Download, ExternalLink, Calendar, Hash, Globe, FileCheck } from 'lucide-react';
import { Receipt } from '../lib/types';
import { Lang, translations } from '../lib/i18n';

interface ReceiptCardProps {
  receipt: Receipt;
  lang: Lang;
}

export const ReceiptCard: React.FC<ReceiptCardProps> = ({ receipt, lang }) => {
  const t = translations[lang];

  const handlePrint = () => {
    window.print();
  };

  return (
    <div className="bg-gradient-to-br from-emerald-500/10 via-white to-sky-500/10 border-2 border-emerald-500/40 rounded-2xl p-6 shadow-xl space-y-5 animate-in fade-in duration-300">
      <div className="flex items-center gap-3">
        <div className="w-12 h-12 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600">
          <CheckCircle2 className="w-8 h-8" />
        </div>
        <div>
          <h2 className="text-lg font-bold text-slate-900">{t.receiptTitle}</h2>
          <p className="text-xs text-slate-600">{receipt.summary || 'Official receipt generated.'}</p>
        </div>
      </div>

      <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-sm space-y-3 text-xs">
        <div className="flex items-center justify-between pb-2 border-b border-slate-100">
          <span className="text-slate-500 flex items-center gap-1.5 font-medium">
            <Hash className="w-4 h-4 text-slate-400" />
            {t.refNumber}
          </span>
          <span className="font-mono font-bold text-sm text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded">
            {receipt.reference_no}
          </span>
        </div>

        <div className="flex items-center justify-between pb-2 border-b border-slate-100">
          <span className="text-slate-500 flex items-center gap-1.5 font-medium">
            <Calendar className="w-4 h-4 text-slate-400" />
            {t.submissionTime}
          </span>
          <span className="font-medium text-slate-800">
            {new Date(receipt.timestamp).toLocaleString()}
          </span>
        </div>

        <div className="flex items-center justify-between">
          <span className="text-slate-500 flex items-center gap-1.5 font-medium">
            <Globe className="w-4 h-4 text-slate-400" />
            {t.portalLabel}
          </span>
          <a
            href={receipt.portal}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sky-600 hover:underline flex items-center gap-1 truncate max-w-xs"
          >
            {receipt.portal} <ExternalLink className="w-3 h-3 flex-shrink-0" />
          </a>
        </div>
      </div>

      {receipt.submitted_fields_masked && (
        <div className="bg-slate-50 rounded-xl p-3 border border-slate-200 text-xs space-y-1">
          <span className="font-semibold text-slate-700 block mb-1">Masked Submission Trail:</span>
          <div className="grid grid-cols-2 gap-2 text-[11px]">
            {Object.entries(receipt.submitted_fields_masked).slice(0, 6).map(([k, v]) => (
              <div key={k} className="text-slate-600">
                <span className="capitalize">{k}: </span>
                <span className="font-mono font-medium text-slate-800">{v}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <button
        onClick={handlePrint}
        className="w-full bg-slate-900 hover:bg-slate-800 text-white font-medium text-xs py-2.5 px-4 rounded-xl flex items-center justify-center gap-2 shadow-sm transition"
      >
        <Download className="w-4 h-4" />
        <span>{t.downloadReceipt}</span>
      </button>
    </div>
  );
};

