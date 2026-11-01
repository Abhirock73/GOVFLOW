import React, { useState, useEffect } from 'react';
import {
  Shield,
  Search,
  Sparkles,
  Trash2,
  Globe,
  Play,
  RotateCcw,
  CheckCircle,
} from 'lucide-react';
import { Lang, translations } from './lib/i18n';
import { CitizenProfile, Job, AgentStep, HumanRequest, Receipt } from './lib/types';
import { GovFlowWebSocket } from './lib/ws';
import { Chat } from './components/Chat';
import { LiveBrowser } from './components/LiveBrowser';
import { ProfileCard } from './components/ProfileCard';
import { StepTimeline } from './components/StepTimeline';
import { PortalConfirmCard } from './components/PortalConfirmCard';
import { ChecklistCard } from './components/ChecklistCard';
import { ConfirmModal } from './components/ConfirmModal';
import { ReceiptCard } from './components/ReceiptCard';

export const App: React.FC = () => {
  const [lang, setLang] = useState<Lang>('en');
  const t = translations[lang];

  const [inputQuery, setInputQuery] = useState('');
  const [job, setJob] = useState<Job | null>(null);
  const [profile, setProfile] = useState<CitizenProfile>({});
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [messages, setMessages] = useState<Array<{ id: string; sender: 'agent' | 'user' | 'system'; text: string; timestamp: string }>>([
    {
      id: 'welcome',
      sender: 'agent',
      text: 'Namaste! I am GovFlow AI. Name a government service (e.g. "apply for Kushal Yuva 2026", "PM-Kisan subsidy") or paste a portal link to begin.',
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    },
  ]);
  const [pendingRequest, setPendingRequest] = useState<HumanRequest | null>(null);
  const [isConfirmModalOpen, setIsConfirmModalOpen] = useState(false);
  const [wsClient, setWsClient] = useState<GovFlowWebSocket | null>(null);
  const [planData, setPlanData] = useState<any>(null);
  const [gapData, setGapData] = useState<any>(null);
  const [resolutionData, setResolutionData] = useState<any>(null);

  // Initialize or update WebSocket on job change
  useEffect(() => {
    if (!job?.job_id) return;

    const ws = new GovFlowWebSocket(job.job_id);
    setWsClient(ws);

    const unsubscribe = ws.subscribe((data) => {
      if (data.type === 'status') {
        setJob((prev) => (prev ? { ...prev, status: data.status, service: data.service || prev.service, error: data.error || prev.error } : prev));
        if (data.resolution) setResolutionData(data.resolution);
        if (data.error) {
          setMessages((prev) => [
            ...prev,
            {
              id: Date.now().toString(),
              sender: 'system',
              text: `⚠️ Notice: ${data.error}`,
              timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
            },
          ]);
        }
      } else if (data.type === 'step') {
        setSteps((prev) => [...prev, data.step]);
      } else if (data.type === 'human_request') {
        setPendingRequest(data.request);
        if (data.request.kind === 'confirm_submit') {
          setIsConfirmModalOpen(true);
        }
      } else if (data.type === 'chat') {
        setMessages((prev) => [
          ...prev,
          {
            id: Date.now().toString(),
            sender: data.sender || 'agent',
            text: data.text,
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          },
        ]);
      } else if (data.type === 'receipt') {
        setJob((prev) => (prev ? { ...prev, receipt: data.receipt, status: 'completed' } : prev));
      } else if (data.type === 'plan_ready') {
        setPlanData(data.plan);
        setGapData(data.gap_report);
      } else if (data.type === 'profile_updated') {
        setProfile(data.profile);
      }
    });

    return () => {
      unsubscribe();
      ws.close();
    };
  }, [job?.job_id]);

  const handleStartApplication = async () => {
    if (!inputQuery.trim()) return;

    const isUrl = inputQuery.startsWith('http://') || inputQuery.startsWith('https://');
    const payload = isUrl ? { url: inputQuery.trim() } : { text: inputQuery.trim() };

    try {
      const res = await fetch('/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `Server returned ${res.status}`);
      }
      const data: Job = await res.json();
      setJob(data);

      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          sender: 'user',
          text: inputQuery.trim(),
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
        {
          id: (Date.now() + 1).toString(),
          sender: 'agent',
          text: `Starting service discovery for "${inputQuery.trim()}". Searching official portal databases...`,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } catch (e: any) {
      console.error('Failed to create job:', e);
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          sender: 'system',
          text: `Failed to start: ${e.message || 'Please check your connection and configuration.'}`,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    }
  };

  const handleConfirmPortal = async () => {
    if (!job || !resolutionData?.candidates?.[0]) return;
    const chosenUrl = resolutionData.candidates[0].url;

    try {
      await fetch(`/jobs/${job.job_id}/confirm-portal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: chosenUrl, confirmed: true }),
      });
      setResolutionData(null);
    } catch (e) {
      console.error('Failed to confirm portal:', e);
    }
  };

  const handleStartBrowser = async () => {
    if (!job) return;
    try {
      await fetch(`/jobs/${job.job_id}/start`, { method: 'POST' });
    } catch (e) {
      console.error('Failed to start browser automation:', e);
    }
  };

  const handleHumanReply = async (reply: { text?: string; approved?: boolean; captcha_solved?: boolean }) => {
    if (!job || !pendingRequest) return;
    try {
      await fetch(`/jobs/${job.job_id}/human-reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: pendingRequest.request_id,
          text: reply.text,
          approved: reply.approved,
          captcha_solved: reply.captcha_solved,
        }),
      });
      setPendingRequest(null);
      setIsConfirmModalOpen(false);
    } catch (e) {
      console.error('Failed to send human reply:', e);
    }
  };

  const handleSaveProfile = async (updated: CitizenProfile) => {
    if (!job) return;
    try {
      const res = await fetch(`/jobs/${job.job_id}/profile`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile: updated }),
      });
      const data = await res.json();
      setProfile(data);
    } catch (e) {
      console.error('Failed to save profile:', e);
    }
  };

  const handleUploadVoice = async (file: File) => {
    if (!job) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('language', lang);

    try {
      const res = await fetch(`/jobs/${job.job_id}/intake/voice`, {
        method: 'POST',
        body: fd,
      });
      const data = await res.json();
      setProfile(data.profile);
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          sender: 'agent',
          text: `Voice note transcribed: "${data.transcript}". Profile updated!`,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } catch (e) {
      console.error('Voice upload failed:', e);
    }
  };

  const handleUploadId = async (file: File) => {
    if (!job) return;
    const fd = new FormData();
    fd.append('file', file);

    try {
      const res = await fetch(`/jobs/${job.job_id}/intake/id`, {
        method: 'POST',
        body: fd,
      });
      const data = await res.json();
      setProfile(data.profile);
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          sender: 'agent',
          text: 'ID document verified and extracted using vision. Profile updated!',
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } catch (e) {
      console.error('ID upload failed:', e);
    }
  };

  const handleDeleteData = async () => {
    if (!job) return;
    if (!confirm('Are you sure you want to delete all personal profile data and files?')) return;

    try {
      await fetch(`/jobs/${job.job_id}/data`, { method: 'DELETE' });
      setJob(null);
      setProfile({});
      setSteps([]);
      setResolutionData(null);
      setPlanData(null);
      setGapData(null);
      setMessages([
        {
          id: Date.now().toString(),
          sender: 'system',
          text: t.dataDeletedNotice,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } catch (e) {
      console.error('Data deletion failed:', e);
    }
  };

  return (
    <div className="min-h-screen flex flex-col bg-slate-50">
      {/* Top Navbar */}
      <header className="bg-white border-b border-slate-200 px-6 py-3.5 flex items-center justify-between sticky top-0 z-30 shadow-xs">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-sky-600 flex items-center justify-center text-white shadow-sm">
            <Shield className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="font-bold text-slate-900 text-lg leading-tight">{t.appTitle}</h1>
              <span className="text-[10px] uppercase font-bold tracking-wider bg-sky-100 text-sky-800 px-1.5 py-0.5 rounded">
                Autonomous Agent
              </span>
            </div>
            <p className="text-xs text-slate-500">{t.tagline}</p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Language Toggle */}
          <button
            onClick={() => setLang(lang === 'en' ? 'hi' : 'en')}
            className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 border border-slate-200 rounded-lg hover:bg-slate-100 text-slate-700 transition"
          >
            <Globe className="w-3.5 h-3.5" />
            <span>{lang === 'en' ? 'हिन्दी' : 'English'}</span>
          </button>

          {/* Delete My Data */}
          {job && (
            <button
              onClick={handleDeleteData}
              className="flex items-center gap-1 text-xs text-rose-600 hover:text-rose-700 hover:bg-rose-50 px-2.5 py-1.5 rounded-lg border border-rose-200 transition font-medium"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>{t.deleteData}</span>
            </button>
          )}
        </div>
      </header>

      {/* Main Single-Input Hero Bar */}
      <div className="bg-white border-b border-slate-200 py-3 px-6 shadow-xs">
        <div className="max-w-5xl mx-auto flex gap-2">
          <div className="relative flex-1">
            <Search className="w-4 h-4 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={inputQuery}
              onChange={(e) => setInputQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleStartApplication()}
              placeholder={t.inputPlaceholder}
              className="w-full text-sm pl-10 pr-4 py-2.5 border border-slate-300 rounded-xl outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-100 transition shadow-2xs"
            />
          </div>
          <button
            onClick={handleStartApplication}
            disabled={!inputQuery.trim()}
            className="bg-sky-600 hover:bg-sky-700 disabled:opacity-40 text-white font-medium text-xs px-5 py-2.5 rounded-xl shadow-sm flex items-center gap-1.5 transition flex-shrink-0"
          >
            <Sparkles className="w-4 h-4" />
            <span>{t.submitButton}</span>
          </button>
        </div>
      </div>

      {/* Dual Pane Layout */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Left Pane: Chat, Cards, Checklist, Timeline */}
        <div className="lg:col-span-6 space-y-4 flex flex-col">
          {/* Portal Confirmation Card if resolver found a portal */}
          {resolutionData?.candidates?.[0] && !job?.receipt && (
            <PortalConfirmCard
              url={resolutionData.candidates[0].url}
              serviceName={resolutionData.candidates[0].title || job?.service?.service_name}
              trust={resolutionData.candidates[0].domain?.trust}
              reason={resolutionData.candidates[0].reason}
              lang={lang}
              onConfirm={handleConfirmPortal}
            />
          )}

          {/* Receipt Card if completed */}
          {job?.receipt && <ReceiptCard receipt={job.receipt} lang={lang} />}

          {/* Checklist Card */}
          {gapData && !job?.receipt && (
            <ChecklistCard
              missingFields={gapData.missing_fields || []}
              missingDocs={gapData.missing_documents || []}
              eligibilityWarnings={gapData.eligibility_warnings || []}
              lang={lang}
              onUploadDoc={() => {}}
            />
          )}

          {/* Resolving Banner */}
          {job && job.status === 'resolving' && (
            <div className="bg-sky-50 border border-sky-200 rounded-xl p-3 flex items-center gap-2.5">
              <div className="w-4 h-4 border-2 border-sky-600 border-t-transparent rounded-full animate-spin flex-shrink-0" />
              <span className="text-xs text-sky-900 font-medium">
                Searching official portal databases and verifying domain authenticity...
              </span>
            </div>
          )}

          {/* Planning Banner */}
          {job && job.status === 'planning' && (
            <div className="bg-sky-50 border border-sky-200 rounded-xl p-3 flex items-center gap-2.5">
              <div className="w-4 h-4 border-2 border-sky-600 border-t-transparent rounded-full animate-spin flex-shrink-0" />
              <span className="text-xs text-sky-900 font-medium">
                Inspecting form structure, required fields, and eligibility rules...
              </span>
            </div>
          )}

          {/* Start Filling Button after gap ready */}
          {job && (job.status === 'intake' || job.status === 'awaiting_missing_data') && (
            <div className="bg-sky-50 border border-sky-200 rounded-xl p-3 flex items-center justify-between">
              <span className="text-xs text-sky-900 font-medium">
                Profile and requirements verified. Ready to start automation?
              </span>
              <button
                onClick={handleStartBrowser}
                className="px-3.5 py-1.5 bg-sky-600 hover:bg-sky-700 text-white rounded-lg text-xs font-semibold flex items-center gap-1 shadow-sm"
              >
                <Play className="w-3.5 h-3.5" />
                <span>Start Filling Live</span>
              </button>
            </div>
          )}

          {/* Running Status Banner */}
          {job && job.status === 'running' && (
            <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-3 flex items-center gap-2">
              <div className="w-2.5 h-2.5 bg-emerald-500 rounded-full animate-ping flex-shrink-0" />
              <span className="text-xs text-emerald-900 font-medium">
                Autonomous browser automation running live. Watching steps in real time...
              </span>
            </div>
          )}

          {/* Profile Card */}
          <ProfileCard profile={profile} lang={lang} onSave={handleSaveProfile} />

          {/* Chat Stream */}
          <Chat
            messages={messages}
            lang={lang}
            onSendMessage={(text) => {
              wsClient?.sendChatMessage(text);
              setMessages((prev) => [
                ...prev,
                {
                  id: Date.now().toString(),
                  sender: 'user',
                  text,
                  timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                },
              ]);
              if (pendingRequest) {
                handleHumanReply({ text });
              }
            }}
            onUploadVoice={handleUploadVoice}
            onUploadId={handleUploadId}
          />

          {/* Live Step Timeline */}
          <StepTimeline steps={steps} lang={lang} />
        </div>

        {/* Right Pane: Live Browser Canvas & Takeover */}
        <div className="lg:col-span-6 h-[720px] lg:sticky lg:top-24">
          <LiveBrowser
            wsClient={wsClient}
            pendingRequest={pendingRequest}
            lang={lang}
            onHumanReply={handleHumanReply}
          />
        </div>
      </main>

      {/* Irreversible Confirm Submit Modal */}
      <ConfirmModal
        isOpen={isConfirmModalOpen}
        summary={pendingRequest?.summary || {}}
        lang={lang}
        onConfirm={() => handleHumanReply({ approved: true })}
        onCancel={() => handleHumanReply({ approved: false })}
      />
    </div>
  );
};

export default App;

