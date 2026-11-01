import React from 'react';
import { ShieldCheck, AlertTriangle, ExternalLink, ArrowRight } from 'lucide-react';
import { Lang, translations } from '../lib/i18n';

interface PortalConfirmCardProps {
  url: string;
  serviceName?: string;
  trust?: string;
  reason?: string;
  lang: Lang;
  onConfirm: () => void;
}

export const PortalConfirmCard: React.FC<PortalConfirmCardProps> = ({
  url,
  serviceName,
  trust,
  reason,
  lang,
  onConfirm,
}) => {
  const t = translations[lang];
  const isOfficial = trust === 'official' || url.includes('.gov.in') || url.includes('.nic.in') || url.includes('localhost') || url.includes('127.0.0.1');

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm space-y-3">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="font-semibold text-slate-900 text-base">
            {serviceName || 'Target Official Portal'}
          </h3>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-sky-600 hover:underline flex items-center gap-1 mt-0.5 break-all"
          >
            {url} <ExternalLink className="w-3 h-3 flex-shrink-0" />
          </a>
        </div>
        <div
          className={`flex items-center gap-1 text-xs px-2.5 py-1 rounded-full font-medium ${
            isOfficial
              ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
              : 'bg-amber-50 text-amber-700 border border-amber-200'
          }`}
        >
          {isOfficial ? <ShieldCheck className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
          {isOfficial ? t.officialDomain : t.unverifiedDomain}
        </div>
      </div>

      {reason && <p className="text-xs text-slate-600 italic">"{reason}"</p>}

      <div className="pt-1">
        <button
          onClick={onConfirm}
          className="w-full bg-sky-600 hover:bg-sky-700 text-white font-medium text-sm py-2 px-4 rounded-lg flex items-center justify-center gap-2 transition"
        >
          <span>Confirm Portal & Plan Form</span>
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
};

