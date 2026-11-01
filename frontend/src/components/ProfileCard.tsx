import React, { useState } from 'react';
import { User, ShieldCheck, Edit3, Save, Check } from 'lucide-react';
import { CitizenProfile } from '../lib/types';
import { Lang, translations } from '../lib/i18n';

interface ProfileCardProps {
  profile: CitizenProfile;
  lang: Lang;
  onSave: (updated: CitizenProfile) => void;
}

export const ProfileCard: React.FC<ProfileCardProps> = ({ profile, lang, onSave }) => {
  const t = translations[lang];
  const [editing, setEditing] = useState(false);
  const [formData, setFormData] = useState<CitizenProfile>(profile);

  React.useEffect(() => {
    setFormData(profile);
  }, [profile]);

  const handleTextChange = (field: keyof CitizenProfile, val: string) => {
    setFormData((prev) => ({
      ...prev,
      [field]: {
        value: val,
        confidence: 1.0,
        source: 'user_edit',
      },
    }));
  };

  const handleSave = () => {
    onSave(formData);
    setEditing(false);
  };

  const renderBadge = (conf?: number, source?: string) => {
    const isHigh = (conf ?? 0) >= 0.85;
    return (
      <div className="flex items-center gap-1">
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded font-mono font-medium ${
            isHigh ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'
          }`}
        >
          {conf ? `${Math.round(conf * 100)}%` : 'Manual'}
        </span>
        {source && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 uppercase">
            {source}
          </span>
        )}
      </div>
    );
  };

  const nameVal = formData.name?.value || '';
  const dobVal = formData.dob?.value || '';
  const idNumVal = formData.id_number?.value || '';
  const phoneVal = formData.phone?.value || '';
  const categoryVal = formData.category?.value || '';
  const addressLine = formData.address?.value
    ? typeof formData.address.value === 'string'
      ? formData.address.value
      : `${formData.address.value.line1 || ''}, ${formData.address.value.district || ''}, ${formData.address.value.state || ''}`
    : '';

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <User className="w-4 h-4 text-sky-600" />
          <h3 className="font-semibold text-slate-900 text-sm">{t.profileTitle}</h3>
        </div>
        <button
          onClick={editing ? handleSave : () => setEditing(true)}
          className="text-xs text-sky-600 hover:text-sky-700 font-medium flex items-center gap-1"
        >
          {editing ? (
            <>
              <Save className="w-3.5 h-3.5" />
              <span>{t.saveProfile}</span>
            </>
          ) : (
            <>
              <Edit3 className="w-3.5 h-3.5" />
              <span>{t.editProfile}</span>
            </>
          )}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <label className="text-slate-500 block mb-0.5">Name</label>
          {editing ? (
            <input
              type="text"
              value={nameVal}
              onChange={(e) => handleTextChange('name', e.target.value)}
              className="w-full border rounded px-2 py-1 text-xs"
            />
          ) : (
            <div className="flex items-center justify-between">
              <span className="font-medium text-slate-900">{nameVal || '—'}</span>
              {renderBadge(profile.name?.confidence, profile.name?.source)}
            </div>
          )}
        </div>

        <div>
          <label className="text-slate-500 block mb-0.5">Date of Birth</label>
          {editing ? (
            <input
              type="text"
              value={dobVal}
              onChange={(e) => handleTextChange('dob', e.target.value)}
              className="w-full border rounded px-2 py-1 text-xs"
            />
          ) : (
            <div className="flex items-center justify-between">
              <span className="font-medium text-slate-900">{dobVal || '—'}</span>
              {renderBadge(profile.dob?.confidence, profile.dob?.source)}
            </div>
          )}
        </div>

        <div>
          <label className="text-slate-500 block mb-0.5">ID Number</label>
          {editing ? (
            <input
              type="text"
              value={idNumVal}
              onChange={(e) => handleTextChange('id_number', e.target.value)}
              className="w-full border rounded px-2 py-1 text-xs"
            />
          ) : (
            <div className="flex items-center justify-between">
              <span className="font-mono text-slate-900">
                {idNumVal ? `•••• •••• ${idNumVal.slice(-4)}` : '—'}
              </span>
              {renderBadge(profile.id_number?.confidence, profile.id_number?.source)}
            </div>
          )}
        </div>

        <div>
          <label className="text-slate-500 block mb-0.5">Category</label>
          {editing ? (
            <input
              type="text"
              value={categoryVal}
              onChange={(e) => handleTextChange('category', e.target.value)}
              className="w-full border rounded px-2 py-1 text-xs"
            />
          ) : (
            <div className="flex items-center justify-between">
              <span className="font-medium text-slate-900">{categoryVal || '—'}</span>
              {renderBadge(profile.category?.confidence, profile.category?.source)}
            </div>
          )}
        </div>

        <div className="col-span-2">
          <label className="text-slate-500 block mb-0.5">Address</label>
          <span className="font-medium text-slate-800 break-words">{addressLine || '—'}</span>
        </div>
      </div>
    </div>
  );
};

