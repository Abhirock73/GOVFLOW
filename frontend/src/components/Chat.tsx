import React, { useState, useRef } from 'react';
import { Send, Mic, Image, Bot, User, Loader2 } from 'lucide-react';
import { Lang, translations } from '../lib/i18n';

interface Message {
  id: string;
  sender: 'agent' | 'user' | 'system';
  text: string;
  timestamp: string;
}

interface ChatProps {
  messages: Message[];
  lang: Lang;
  onSendMessage: (text: string) => void;
  onUploadVoice: (file: File) => void;
  onUploadId: (file: File) => void;
}

export const Chat: React.FC<ChatProps> = ({
  messages,
  lang,
  onSendMessage,
  onUploadVoice,
  onUploadId,
}) => {
  const t = translations[lang];
  const [input, setInput] = useState('');
  const voiceInputRef = useRef<HTMLInputElement>(null);
  const idInputRef = useRef<HTMLInputElement>(null);

  const handleSend = () => {
    if (!input.trim()) return;
    onSendMessage(input.trim());
    setInput('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="bg-white border border-slate-200 rounded-xl shadow-sm flex flex-col h-[380px] overflow-hidden">
      {/* Messages list */}
      <div className="flex-1 p-3 overflow-y-auto space-y-3">
        {messages.map((m) => {
          const isAgent = m.sender === 'agent' || m.sender === 'system';
          return (
            <div
              key={m.id}
              className={`flex items-start gap-2 ${isAgent ? '' : 'flex-row-reverse'}`}
            >
              <div
                className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 text-xs ${
                  isAgent ? 'bg-sky-100 text-sky-700' : 'bg-slate-800 text-white'
                }`}
              >
                {isAgent ? <Bot className="w-3.5 h-3.5" /> : <User className="w-3.5 h-3.5" />}
              </div>
              <div
                className={`rounded-xl px-3 py-2 text-xs max-w-[80%] ${
                  isAgent
                    ? 'bg-slate-100 text-slate-800 rounded-tl-none'
                    : 'bg-sky-600 text-white rounded-tr-none'
                }`}
              >
                <p className="whitespace-pre-wrap">{m.text}</p>
                <span
                  className={`text-[9px] block mt-1 ${
                    isAgent ? 'text-slate-400' : 'text-sky-200'
                  }`}
                >
                  {m.timestamp}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Intake shortcut buttons */}
      <div className="px-3 py-1.5 bg-slate-50 border-t border-slate-100 flex items-center gap-2">
        <input
          type="file"
          ref={voiceInputRef}
          accept="audio/*"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && onUploadVoice(e.target.files[0])}
        />
        <button
          onClick={() => voiceInputRef.current?.click()}
          className="px-2 py-1 bg-white hover:bg-slate-100 border border-slate-200 rounded text-[11px] text-slate-600 flex items-center gap-1 transition"
        >
          <Mic className="w-3 h-3 text-sky-600" />
          <span>{t.uploadVoice}</span>
        </button>

        <input
          type="file"
          ref={idInputRef}
          accept="image/*,application/pdf,.heic,.heif"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && onUploadId(e.target.files[0])}
        />
        <button
          onClick={() => idInputRef.current?.click()}
          className="px-2 py-1 bg-white hover:bg-slate-100 border border-slate-200 rounded text-[11px] text-slate-600 flex items-center gap-1 transition"
        >
          <Image className="w-3 h-3 text-emerald-600" />
          <span>{t.uploadId}</span>
        </button>
      </div>

      {/* Input row */}
      <div className="p-2 border-t border-slate-200 bg-white flex items-center gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type an answer or message..."
          className="flex-1 text-xs border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-1 focus:ring-sky-500"
        />
        <button
          onClick={handleSend}
          disabled={!input.trim()}
          className="p-2 bg-sky-600 hover:bg-sky-700 disabled:opacity-40 text-white rounded-lg transition"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
};

