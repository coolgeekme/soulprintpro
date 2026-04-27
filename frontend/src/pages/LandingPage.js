import React, { useState, useMemo, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Mail, ChevronLeft, ChevronRight } from 'lucide-react';
import { agents } from '../data/mock';

// Matrix background
const MatrixBg = () => {
  const grid = useMemo(() => {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+=-><';
    return Array.from({ length: 22 }, () =>
      Array.from({ length: 15 }, () => chars[Math.floor(Math.random() * chars.length)])
    );
  }, []);
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none select-none" aria-hidden="true">
      {grid.map((row, r) => (
        <div key={r} className="flex justify-between px-5" style={{ lineHeight: '2.4rem' }}>
          {row.map((ch, c) => (
            <span key={c} style={{ color: 'rgba(0,0,0,0.09)', fontSize: '11px', fontFamily: 'monospace' }}>{ch}</span>
          ))}
        </div>
      ))}
    </div>
  );
};

// Wingman mascot SVG
const WingmanMascot = () => (
  <svg width="68" height="68" viewBox="0 0 68 68" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect x="3" y="7" width="62" height="44" rx="13" fill="#EFEFEF" stroke="#DCDCDC" strokeWidth="1.5"/>
    <path d="M14 51 L6 65 L26 51" fill="#EFEFEF" stroke="#DCDCDC" strokeWidth="1.5" strokeLinejoin="round"/>
    <circle cx="23" cy="29" r="7" fill="#1F1F1F"/>
    <circle cx="45" cy="29" r="7" fill="#1F1F1F"/>
    <circle cx="25.5" cy="27" r="2.5" fill="white"/>
    <circle cx="47.5" cy="27" r="2.5" fill="white"/>
  </svg>
);

// Google SVG
const GoogleIcon = () => (
  <svg viewBox="0 0 24 24" width="19" height="19">
    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
  </svg>
);

// GitHub SVG
const GithubIcon = () => (
  <svg viewBox="0 0 24 24" width="19" height="19" fill="white">
    <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"/>
  </svg>
);

// Apple SVG
const AppleIcon = () => (
  <svg viewBox="0 0 24 24" width="19" height="19" fill="white">
    <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
  </svg>
);

// Facebook SVG
const FacebookIcon = () => (
  <svg viewBox="0 0 24 24" width="19" height="19" fill="white">
    <path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/>
  </svg>
);

const LandingPage = () => {
  const navigate = useNavigate();
  const [showEmail, setShowEmail] = useState(false);
  const [email, setEmail] = useState('');
  const [currentSlide, setCurrentSlide] = useState(0);
  const carouselRef = useRef(null);
  const cardRefs = useRef([]);

  // Auto-advance carousel
  useEffect(() => {
    const interval = setInterval(() => {
      setCurrentSlide(prev => (prev + 1) % agents.length);
    }, 3500);
    return () => clearInterval(interval);
  }, []);

  // Scroll carousel on slide change
  useEffect(() => {
    if (!carouselRef.current) return;
    const card = cardRefs.current[currentSlide];
    if (card) {
      carouselRef.current.scrollTo({
        left: card.offsetLeft - 24,
        behavior: 'smooth',
      });
    }
  }, [currentSlide]);

  const handlePrev = () => setCurrentSlide(p => (p - 1 + agents.length) % agents.length);
  const handleNext = () => setCurrentSlide(p => (p + 1) % agents.length);

  const handleLogin = () => {
    localStorage.setItem('wingman_user', 'true');
    navigate('/dashboard');
  };

  return (
    <div className="flex h-screen overflow-hidden" style={{ fontFamily: "'Inter', sans-serif" }}>

      {/* ── LEFT PANEL ── */}
      <div className="relative bg-white flex flex-col overflow-hidden" style={{ width: '42%', minWidth: '360px' }}>
        <MatrixBg />

        {/* Logo */}
        <div className="relative z-10 px-7 pt-6">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-md bg-gray-900 flex items-center justify-center">
              <div className="w-3 h-3 rounded-sm bg-white" />
            </div>
            <span className="text-gray-900 font-semibold text-lg tracking-tight">emergent</span>
          </div>
        </div>

        {/* Form */}
        <div className="relative z-10 flex flex-col items-center justify-center flex-1 px-14 pb-10">
          <WingmanMascot />

          <h1 className="mt-5 text-[1.6rem] font-bold text-gray-950 text-center tracking-tight">
            Introducing Wingman
          </h1>
          <p className="mt-1 text-[1.1rem] font-semibold text-center" style={{ color: '#06B6D4' }}>
            built for getting things done!
          </p>

          <div className="w-full mt-8 space-y-3 max-w-xs">
            {/* Google */}
            <button
              onClick={handleLogin}
              className="w-full flex items-center justify-center gap-3 py-[11px] px-4 rounded-2xl text-white font-medium text-sm transition-all duration-150 hover:opacity-85 active:scale-[0.98]"
              style={{ backgroundColor: '#0D0D0D' }}
            >
              <GoogleIcon />
              Continue with Google
            </button>

            {/* Social trio */}
            <div className="flex gap-3">
              {[
                { icon: <GithubIcon />, label: 'GitHub' },
                { icon: <AppleIcon />, label: 'Apple' },
                { icon: <FacebookIcon />, label: 'Facebook' },
              ].map(({ icon, label }) => (
                <button
                  key={label}
                  onClick={handleLogin}
                  aria-label={`Continue with ${label}`}
                  className="flex-1 flex items-center justify-center py-[11px] rounded-2xl transition-all duration-150 hover:opacity-85 active:scale-[0.98]"
                  style={{ backgroundColor: '#0D0D0D' }}
                >
                  {icon}
                </button>
              ))}
            </div>

            {/* Email */}
            {!showEmail ? (
              <button
                onClick={() => setShowEmail(true)}
                className="w-full flex items-center justify-center gap-3 py-[11px] px-4 rounded-2xl border border-gray-200 text-gray-700 font-medium text-sm bg-white transition-all duration-150 hover:bg-gray-50 hover:border-gray-300 active:scale-[0.98]"
              >
                <Mail className="w-[18px] h-[18px] text-gray-500" />
                Continue with Email
              </button>
            ) : (
              <div className="space-y-2">
                <input
                  type="email"
                  placeholder="Enter your email"
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  autoFocus
                  className="w-full px-4 py-[11px] rounded-2xl border border-gray-200 text-sm text-gray-900 placeholder-gray-400 outline-none focus:border-cyan-400 transition-colors"
                />
                <button
                  onClick={handleLogin}
                  className="w-full py-[11px] rounded-2xl text-white text-sm font-medium transition-all duration-150 hover:opacity-90 active:scale-[0.98]"
                  style={{ backgroundColor: '#06B6D4' }}
                >
                  Continue
                </button>
              </div>
            )}
          </div>

          <p className="mt-6 text-[11px] text-gray-400 text-center leading-relaxed">
            By continuing, you agree to our{' '}
            <a href="#" className="text-blue-500 hover:underline" onClick={e => e.preventDefault()}>Terms of Service</a>{' '}and{' '}
            <a href="#" className="text-blue-500 hover:underline" onClick={e => e.preventDefault()}>Privacy Policy</a>
          </p>
        </div>
      </div>

      {/* ── RIGHT PANEL ── */}
      <div
        className="flex-1 flex flex-col overflow-hidden"
        style={{
          background: `
            radial-gradient(ellipse 90% 55% at 20% -5%, rgba(0,224,204,0.88) 0%, transparent 55%),
            radial-gradient(ellipse 65% 45% at 85% 2%, rgba(0,128,230,0.72) 0%, transparent 48%),
            radial-gradient(ellipse 110% 45% at 50% 38%, rgba(0,190,170,0.32) 0%, transparent 60%),
            #071420
          `
        }}
      >
        {/* Headline */}
        <div className="text-center pt-12 pb-4 px-8">
          <h2 className="text-white font-bold leading-[1.1]" style={{ fontSize: '3.1rem', letterSpacing: '-0.02em' }}>
            One assistant
          </h2>
          <h2 className="text-white font-bold leading-[1.1]" style={{ fontSize: '3.1rem', letterSpacing: '-0.02em' }}>
            Everything you need
          </h2>
        </div>

        {/* Carousel */}
        <div className="flex flex-col justify-center px-6 pb-6" style={{ flex: '1 1 0', minHeight: 0 }}>
          <div
            ref={carouselRef}
            className="flex gap-4 items-start carousel-hide-scrollbar"
            style={{
              overflowX: 'auto',
              scrollBehavior: 'smooth',
              msOverflowStyle: 'none',
              WebkitOverflowScrolling: 'touch',
            }}
          >
            {agents.map((agent, i) => (
              <div
                key={agent.id}
                ref={el => (cardRefs.current[i] = el)}
                className="bg-white rounded-2xl p-6 flex-shrink-0 flex flex-col cursor-pointer transition-transform duration-200 hover:-translate-y-1"
                style={{ width: 'calc(31% - 6px)', minWidth: '220px' }}
              >
                {/* Avatar */}
                <div
                  className="w-[72px] h-[72px] rounded-full flex items-center justify-center overflow-hidden mb-4 flex-shrink-0"
                  style={{ backgroundColor: agent.avatarBg }}
                >
                  <img
                    src={agent.avatar}
                    alt={agent.name}
                    className="w-14 h-14"
                    onError={e => { e.target.style.display = 'none'; }}
                  />
                </div>
                <p className="text-[17px] font-bold text-gray-950">{agent.name}</p>
                <p className="text-[13px] font-semibold mt-0.5 mb-3" style={{ color: agent.roleColor }}>{agent.role}</p>
                <p className="text-[13px] text-gray-500 leading-relaxed flex-1">{agent.description}</p>
              </div>
            ))}
          </div>

          {/* Navigation */}
          <div className="flex items-center justify-center gap-3 mt-5">
            <button
              onClick={handlePrev}
              className="w-8 h-8 rounded-full flex items-center justify-center transition-colors hover:bg-white/20"
              style={{ backgroundColor: 'rgba(255,255,255,0.15)' }}
            >
              <ChevronLeft className="w-4 h-4 text-white" />
            </button>

            <div className="flex items-center gap-2">
              {agents.map((_, i) => (
                <button
                  key={i}
                  onClick={() => setCurrentSlide(i)}
                  className="rounded-full transition-all duration-300"
                  style={{
                    width: i === currentSlide ? '22px' : '8px',
                    height: '8px',
                    backgroundColor: i === currentSlide ? '#ffffff' : 'rgba(255,255,255,0.38)',
                  }}
                />
              ))}
            </div>

            <button
              onClick={handleNext}
              className="w-8 h-8 rounded-full flex items-center justify-center transition-colors hover:bg-white/20"
              style={{ backgroundColor: 'rgba(255,255,255,0.15)' }}
            >
              <ChevronRight className="w-4 h-4 text-white" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default LandingPage;
