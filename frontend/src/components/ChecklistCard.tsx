import React from 'react';
import { CheckCircle2, Circle, AlertCircle, FileText, Upload } from 'lucide-react';
import { Lang, translations } from '../lib/i18n';

interface ChecklistCardProps {
  missingFields: Array<{ key: string; label: string; reason?: string }>;
  missingDocs: Array<{ requirement_key: string; name: string; problem: string; detail?: string }>;
  eligibilityWarnings: Array<{ message: string; severity: string }>;
  lang: Lang;
  onUploadDoc: (docKey: string) => void;
}

export const ChecklistCard: React.FC<ChecklistCardProps> = ({
  missingFields,
  missingDocs,
  eligibilityWarnings,
  lang,
  onUploadDoc,
}) => {
  const t = translations[lang];
  const allClear = missingFields.length === 0 && missingDocs.length === 0;

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-slate-900 text-sm flex items-center gap-2">
          <FileText className="w-4 h-4 text-sky-600" />
          {t.checklistTitle}
        </h3>
        <span
          className={`text-xs px-2 py-0.5 rounded-full font-medium ${
            allClear ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'
          }`}
        >
          {allClear ? 'Ready' : `${missingFields.length + missingDocs.length} items needed`}
        </span>
      </div>

      {eligibilityWarnings.length > 0 && (
        <div className="space-y-1.5">
          {eligibilityWarnings.map((w, idx) => (
            <div
              key={idx}
              className="text-xs p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 flex items-start gap-2"
            >
              <AlertCircle className="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
              <span>{w.message}</span>
            </div>
          ))}
        </div>
      )}

      <div className="space-y-2">
        {missingFields.map((f) => (
          <div key={f.key} className="flex items-start gap-2 text-xs text-slate-700">
            <Circle className="w-3.5 h-3.5 text-slate-400 flex-shrink-0 mt-0.5" />
            <div>
              <span className="font-medium">{f.label}</span>
              {f.reason && <p className="text-slate-500 text-[11px]">{f.reason}</p>}
            </div>
          </div>
        ))}

        {missingDocs.map((d) => (
          <div key={d.requirement_key} className="flex items-center justify-between text-xs py-1">
            <div className="flex items-start gap-2 text-slate-700">
              <Circle className="w-3.5 h-3.5 text-amber-500 flex-shrink-0 mt-0.5" />
              <div>
                <span className="font-medium">{d.name}</span>
                {d.detail && <p className="text-slate-500 text-[11px]">{d.detail}</p>}
              </div>
            </div>
            <button
              onClick={() => onUploadDoc(d.requirement_key)}
              className="px-2 py-1 bg-sky-50 text-sky-600 hover:bg-sky-100 rounded text-xs flex items-center gap-1 font-medium"
            >
              <Upload className="w-3 h-3" />
              <span>Upload</span>
            </button>
          </div>
        ))}

        {allClear && (
          <div className="flex items-center gap-2 text-xs text-emerald-700 pt-1">
            <CheckCircle2 className="w-4 h-4 text-emerald-600" />
            <span>All required fields and documents are verified! Ready to fill.</span>
          </div>
        )}
      </div>
    </div>
  );
};

