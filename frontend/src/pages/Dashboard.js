import React, { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Send, Plus, LogOut, Search,
  Zap, Bell, CheckCheck,
  MoreVertical, X
} from 'lucide-react';
import { agents, agentResponses, integrationsList } from '../data/mock';
import { useAuth } from '../context/AuthContext';

// Small status dot
const StatusDot = ({ active }) => (
  <span
    className="inline-block w-2 h-2 rounded-full"
    style={{ backgroundColor: active ? '#22C55E' : '#D1D5DB' }}
  />
);

// Agent avatar
const AgentAvatar = ({ agent, size = 'md' }) => {
  const dim = size === 'sm' ? 32 : size === 'lg' ? 48 : 40;
  const imgDim = size === 'sm' ? 24 : size === 'lg' ? 38 : 32;
  return (
    <div
      className="rounded-full flex-shrink-0 overflow-hidden flex items-center justify-center"
      style={{ width: dim, height: dim, backgroundColor: agent.avatarBg }}
    >
      <img
        src={agent.avatar}
        alt={agent.name}
        style={{ width: imgDim, height: imgDim }}
        onError={e => { e.target.style.display = 'none'; }}
      />
    </div>
  );
};

const IntegrationsPanel = ({ onClose }) => (
  <div className="absolute top-0 right-0 h-full w-80 bg-white border-l border-gray-100 shadow-xl z-50 flex flex-col">
    <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
      <div>
        <h3 className="font-semibold text-gray-900">Integrations</h3>
        <p className="text-xs text-gray-400 mt-0.5">Connect your tools</p>
      </div>
      <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors">
        <X className="w-4 h-4 text-gray-500" />
      </button>
    </div>
    <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
      {integrationsList.map(item => (
        <div key={item.id} className="flex items-center justify-between p-3 rounded-xl border border-gray-100 hover:border-gray-200 transition-colors">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gray-50 flex items-center justify-center">
              <Zap className="w-4 h-4 text-gray-600" />
            </div>
            <div>
              <p className="text-sm font-medium text-gray-900">{item.name}</p>
              <p className="text-xs text-gray-400">{item.category}</p>
            </div>
          </div>
          {item.connected ? (
            <span className="text-xs font-medium text-green-600 bg-green-50 px-2.5 py-1 rounded-full">Connected</span>
          ) : (
            <button className="text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 px-2.5 py-1 rounded-full transition-colors">Connect</button>
          )}
        </div>
      ))}
    </div>
  </div>
);

const Dashboard = () => {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [selectedAgent, setSelectedAgent] = useState(agents[0]);
  const [allMessages, setAllMessages] = useState(
    Object.fromEntries(agents.map(a => [a.id, [...(a.messages || [])]]))  
  );
  const [input, setInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const [showIntegrations, setShowIntegrations] = useState(false);
  const [search, setSearch] = useState('');
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  const messages = allMessages[selectedAgent.id] || [];

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isTyping]);

  const handleAgentSelect = agent => {
    setSelectedAgent(agent);
    setShowIntegrations(false);
  };

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    const msg = {
      id: Date.now(),
      sender: 'user',
      text,
      time: new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }),
    };
    setAllMessages(prev => ({ ...prev, [selectedAgent.id]: [...(prev[selectedAgent.id] || []), msg] }));
    setInput('');
    setIsTyping(true);
    const pool = agentResponses[selectedAgent.id] || ["Got it! Working on that now."];
    const reply = pool[Math.floor(Math.random() * pool.length)];
    setTimeout(() => {
      setIsTyping(false);
      const agentMsg = {
        id: Date.now() + 1,
        sender: 'agent',
        text: reply,
        time: new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }),
      };
      setAllMessages(prev => ({ ...prev, [selectedAgent.id]: [...(prev[selectedAgent.id] || []), agentMsg] }));
    }, 1400 + Math.random() * 800);
  };

  const handleKey = e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  const handleLogout = async () => {
    await logout();
    navigate('/');
  };

  const filteredAgents = agents.filter(a =>
    a.name.toLowerCase().includes(search.toLowerCase()) ||
    a.role.toLowerCase().includes(search.toLowerCase())
  );

  const lastMsg = agent => {
    const msgs = allMessages[agent.id] || [];
    return msgs[msgs.length - 1] || null;
  };

  const unreadCount = agent => {
    return (allMessages[agent.id] || []).filter(m => m.sender === 'agent' && !m.read).length;
  };

  return (
    <div className="flex h-screen bg-gray-50 overflow-hidden" style={{ fontFamily: "'Inter', sans-serif" }}>

      {/* ── SIDEBAR ── */}
      <div className="w-[280px] bg-white border-r border-gray-100 flex flex-col flex-shrink-0">
        {/* Header */}
        <div className="px-5 py-4 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-gray-950 flex items-center justify-center">
              <div className="w-3.5 h-3.5 rounded-sm bg-white" />
            </div>
            <div className="flex items-baseline gap-1">
              <span className="text-gray-950 font-semibold text-[17px] tracking-tight">emergent</span>
              <span className="text-[11px] text-gray-400 font-medium">wingman</span>
            </div>
          </div>
        </div>

        {/* Search */}
        <div className="px-4 pt-3 pb-1">
          <div className="flex items-center gap-2 bg-gray-50 rounded-xl px-3 py-2 border border-gray-100">
            <Search className="w-[15px] h-[15px] text-gray-400" />
            <input
              type="text"
              placeholder="Search agents..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="bg-transparent text-[13px] text-gray-700 placeholder-gray-400 flex-1 outline-none"
            />
          </div>
        </div>

        {/* Agent List */}
        <div className="flex-1 overflow-y-auto px-3 pt-2 space-y-0.5">
          <p className="text-[10px] font-bold text-gray-400 px-2 py-2 uppercase tracking-widest">Your Agents</p>
          {filteredAgents.map(agent => {
            const last = lastMsg(agent);
            const unread = unreadCount(agent);
            const active = selectedAgent.id === agent.id;
            return (
              <button
                key={agent.id}
                onClick={() => handleAgentSelect(agent)}
                className={`w-full flex items-start gap-3 p-3 rounded-xl text-left transition-all duration-150 ${
                  active ? 'bg-gray-950' : 'hover:bg-gray-50'
                }`}
              >
                <AgentAvatar agent={agent} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-1">
                    <span className={`text-[13px] font-semibold truncate ${active ? 'text-white' : 'text-gray-900'}`}>
                      {agent.name}
                    </span>
                    {last && (
                      <span className={`text-[10px] flex-shrink-0 ${active ? 'text-gray-400' : 'text-gray-400'}`}>
                        {last.time}
                      </span>
                    )}
                  </div>
                  <p className={`text-[11px] truncate mt-0.5 ${active ? 'text-gray-400' : 'text-gray-500'}`}>
                    {last ? last.text : 'No messages yet'}
                  </p>
                </div>
                {unread > 0 && (
                  <span className="w-5 h-5 rounded-full flex-shrink-0 text-[10px] font-bold text-white flex items-center justify-center" style={{ backgroundColor: '#06B6D4' }}>
                    {unread}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Add Agent */}
        <div className="px-3 pb-3">
          <button className="w-full flex items-center gap-2 p-3 rounded-xl border border-dashed border-gray-200 text-gray-400 text-[13px] hover:border-gray-300 hover:text-gray-600 transition-colors">
            <Plus className="w-4 h-4" />
            Add new agent
          </button>
        </div>

        {/* User footer */}
        <div className="px-4 py-3 border-t border-gray-100 flex items-center gap-3">
          <div className="w-9 h-9 rounded-full bg-gray-200 flex items-center justify-center text-gray-600 font-bold text-sm flex-shrink-0">
            {user?.name ? user.name[0].toUpperCase() : 'U'}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-[13px] font-semibold text-gray-900 truncate">{user?.name || 'You'}</p>
            <p className="text-[11px] text-gray-400 truncate">{user?.email || 'Personal workspace'}</p>
          </div>
          <button onClick={handleLogout} title="Sign out" className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors">
            <LogOut className="w-4 h-4 text-gray-400" />
          </button>
        </div>
      </div>

      {/* ── MAIN AREA ── */}
      <div className="flex-1 flex flex-col relative overflow-hidden">

        {/* Chat Header */}
        <div className="bg-white border-b border-gray-100 px-6 py-3.5 flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-3">
            <AgentAvatar agent={selectedAgent} size="lg" />
            <div>
              <div className="flex items-center gap-2">
                <h2 className="font-bold text-gray-950 text-[15px]">{selectedAgent.name}</h2>
                <StatusDot active />
                <span className="text-[11px] text-gray-400">Online · Autonomous</span>
              </div>
              <p className="text-[12px] font-semibold mt-0.5" style={{ color: selectedAgent.roleColor }}>
                {selectedAgent.role}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="hidden sm:flex items-center gap-1 mr-1">
              {selectedAgent.integrations.map(name => (
                <span key={name} className="text-[11px] bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full font-medium">
                  {name}
                </span>
              ))}
            </div>
            <button className="p-2 rounded-xl hover:bg-gray-100 transition-colors">
              <Bell className="w-[18px] h-[18px] text-gray-500" />
            </button>
            <button className="p-2 rounded-xl hover:bg-gray-100 transition-colors">
              <MoreVertical className="w-[18px] h-[18px] text-gray-500" />
            </button>
            <button
              onClick={() => setShowIntegrations(p => !p)}
              className="flex items-center gap-1.5 px-3 py-2 text-white text-[13px] font-medium rounded-xl transition-colors hover:opacity-85"
              style={{ backgroundColor: '#0D0D0D' }}
            >
              <Zap className="w-[14px] h-[14px]" />
              Integrations
            </button>
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center">
              <AgentAvatar agent={selectedAgent} size="lg" />
              <p className="mt-4 text-[15px] font-semibold text-gray-900">Start a conversation with {selectedAgent.name}</p>
              <p className="mt-1 text-sm text-gray-400 max-w-xs">{selectedAgent.description}</p>
            </div>
          )}
          {messages.map(msg => (
            <div key={msg.id} className={`flex ${msg.sender === 'user' ? 'justify-end' : 'justify-start'} items-end gap-2.5`}>
              {msg.sender === 'agent' && <AgentAvatar agent={selectedAgent} size="sm" />}
              <div className={`max-w-[68%]`}>
                <div
                  className={`px-4 py-2.5 text-[13.5px] leading-relaxed ${
                    msg.sender === 'user'
                      ? 'bg-gray-950 text-white rounded-2xl rounded-br-sm'
                      : 'bg-white text-gray-900 rounded-2xl rounded-bl-sm shadow-sm border border-gray-100'
                  }`}
                >
                  {msg.text}
                </div>
                <div className={`flex items-center gap-1 mt-1 ${msg.sender === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <span className="text-[10px] text-gray-400">{msg.time}</span>
                  {msg.sender === 'user' && <CheckCheck className="w-3 h-3 text-gray-400" />}
                </div>
              </div>
            </div>
          ))}

          {/* Typing indicator */}
          {isTyping && (
            <div className="flex items-end gap-2.5">
              <AgentAvatar agent={selectedAgent} size="sm" />
              <div className="bg-white rounded-2xl rounded-bl-sm px-4 py-3 shadow-sm border border-gray-100">
                <div className="flex gap-1 items-center">
                  {[0, 150, 300].map(delay => (
                    <span
                      key={delay}
                      className="w-2 h-2 rounded-full bg-gray-300 animate-bounce"
                      style={{ animationDelay: `${delay}ms` }}
                    />
                  ))}
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="bg-white border-t border-gray-100 px-5 py-4 flex-shrink-0">
          <div className="flex items-end gap-3 bg-gray-50 rounded-2xl px-4 py-3 border border-gray-200 focus-within:border-gray-300 transition-colors">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => {
                setInput(e.target.value);
                e.target.style.height = 'auto';
                e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
              }}
              onKeyDown={handleKey}
              placeholder={`Message ${selectedAgent.name}...`}
              rows={1}
              className="flex-1 bg-transparent resize-none text-[13.5px] text-gray-900 placeholder-gray-400 outline-none"
              style={{ maxHeight: '120px' }}
            />
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 transition-all duration-150 disabled:opacity-30 disabled:cursor-not-allowed hover:opacity-80"
              style={{ backgroundColor: input.trim() ? '#0D0D0D' : '#D1D5DB' }}
            >
              <Send className="w-[16px] h-[16px] text-white" />
            </button>
          </div>
          <p className="text-[11px] text-gray-400 text-center mt-2">
            {selectedAgent.name} works autonomously · Press Enter to send
          </p>
        </div>

        {/* Integrations overlay */}
        {showIntegrations && <IntegrationsPanel onClose={() => setShowIntegrations(false)} />}
      </div>
    </div>
  );
};

export default Dashboard;
