export const agents = [
  {
    id: 'jarvis',
    name: 'Jarvis',
    role: 'Tech co-founder',
    roleColor: '#F97316',
    avatarBg: '#FFF3E0',
    description: 'Reviews your code, architects your systems, and ships features while you sleep.',
    integrations: ['GitHub', 'Slack', 'Linear'],
    avatar: 'https://api.dicebear.com/7.x/bottts/svg?seed=jarvis',
    messages: [
      { id: 1, sender: 'agent', text: "Hey! I noticed you pushed to main without tests again. Want me to set up a pre-commit hook to prevent that?", time: '9:41 AM', read: true },
      { id: 2, sender: 'user', text: "Yeah that'd be great, also can you check the deploy logs?", time: '9:43 AM', read: true },
      { id: 3, sender: 'agent', text: "Already on it. Found 3 failed tests in the auth module. I've drafted a fix — should I open a PR?", time: '9:44 AM', read: true },
      { id: 4, sender: 'user', text: "Yes, go ahead and open the PR.", time: '9:45 AM', read: true },
      { id: 5, sender: 'agent', text: "PR #47 opened! I've also added test coverage and a summary for the reviewer. Deploy logs show the issue was a missing env variable — I've updated the docs.", time: '9:46 AM', read: true },
    ]
  },
  {
    id: 'judy',
    name: 'Judy',
    role: 'Sales lead',
    roleColor: '#22C55E',
    avatarBg: '#F0FDF4',
    description: 'Tracks your pipeline, preps your pitches, and follows up so you can focus on closing.',
    integrations: ['HubSpot', 'Gmail', 'LinkedIn'],
    avatar: 'https://api.dicebear.com/7.x/bottts/svg?seed=judy',
    messages: [
      { id: 1, sender: 'agent', text: "Good morning! You have 3 prospects from yesterday's demo who haven't responded. Want me to send follow-ups?", time: '8:30 AM', read: true },
      { id: 2, sender: 'user', text: "Yes, use the standard follow-up template but personalize for each.", time: '8:45 AM', read: true },
      { id: 3, sender: 'agent', text: "Done! Sent personalized follow-ups to Acme Corp, TechStart, and BlueSky. I also updated their statuses in HubSpot to 'Awaiting Response'.", time: '8:46 AM', read: true },
    ]
  },
  {
    id: 'alfred',
    name: 'Alfred',
    role: 'Executive assistant',
    roleColor: '#6B7280',
    avatarBg: '#F3F4F6',
    description: 'Manages your calendar, drafts your emails, and makes sure nothing slips through the cracks.',
    integrations: ['Gmail', 'Google Calendar', 'Outlook'],
    avatar: 'https://api.dicebear.com/7.x/bottts/svg?seed=alfred',
    messages: [
      { id: 1, sender: 'agent', text: "You have a busy day ahead. Board meeting at 10 AM, lunch with Sarah at 1 PM. I've prepared briefing docs for the board meeting.", time: '7:00 AM', read: true },
      { id: 2, sender: 'user', text: "Can you reschedule my 3 PM call to tomorrow?", time: '7:15 AM', read: true },
      { id: 3, sender: 'agent', text: "Done. Rescheduled your call with Marcus to tomorrow at 3 PM. He's been notified and confirmed the new time.", time: '7:15 AM', read: true },
    ]
  },
  {
    id: 'vera',
    name: 'Vera',
    role: 'Content creator',
    roleColor: '#8B5CF6',
    avatarBg: '#F5F3FF',
    description: 'Writes your posts, crafts compelling copy, and keeps your brand voice consistent across platforms.',
    integrations: ['Twitter', 'LinkedIn', 'Instagram'],
    avatar: 'https://api.dicebear.com/7.x/bottts/svg?seed=vera',
    messages: [
      { id: 1, sender: 'agent', text: "I've drafted 5 LinkedIn posts based on your latest blog. Want me to schedule them throughout the week?", time: '10:00 AM', read: false },
    ]
  },
  {
    id: 'max',
    name: 'Max',
    role: 'Research analyst',
    roleColor: '#3B82F6',
    avatarBg: '#EFF6FF',
    description: 'Digs through data, compiles reports, and surfaces insights so you can make faster decisions.',
    integrations: ['Google', 'Notion', 'Slack'],
    avatar: 'https://api.dicebear.com/7.x/bottts/svg?seed=max',
    messages: [
      { id: 1, sender: 'agent', text: "Competitive analysis on your top 3 competitors is ready. Key finding: they all lack async collaboration features — a gap you can own.", time: '11:00 AM', read: false },
    ]
  },
];

export const integrationsList = [
  { id: 'gmail', name: 'Gmail', connected: true, category: 'Email', icon: 'Mail' },
  { id: 'gcal', name: 'Google Calendar', connected: true, category: 'Calendar', icon: 'Calendar' },
  { id: 'slack', name: 'Slack', connected: true, category: 'Communication', icon: 'MessageSquare' },
  { id: 'github', name: 'GitHub', connected: true, category: 'Development', icon: 'Code' },
  { id: 'hubspot', name: 'HubSpot', connected: false, category: 'CRM', icon: 'BarChart2' },
  { id: 'notion', name: 'Notion', connected: false, category: 'Productivity', icon: 'FileText' },
  { id: 'linkedin', name: 'LinkedIn', connected: false, category: 'Social', icon: 'Users' },
  { id: 'whatsapp', name: 'WhatsApp', connected: false, category: 'Messaging', icon: 'MessageCircle' },
];

export const agentResponses = {
  jarvis: [
    "I'm scanning the codebase now. Give me a moment to check for issues.",
    "Found it! There's a race condition in the API handler. I've drafted a fix — want me to open a PR?",
    "Tests are passing now. Deploying to staging for review.",
    "Done! I've also improved the error handling while I was in there.",
  ],
  judy: [
    "On it! I'll research the prospect and personalize the outreach.",
    "Sent! I also found 3 warm leads from last month's conference that haven't been followed up yet.",
    "The prospect replied positively! I've scheduled a demo for Thursday at 2 PM.",
    "Pipeline updated. You're at 78% of your monthly quota — strong position.",
  ],
  alfred: [
    "Consider it done! I've updated your calendar and notified all attendees.",
    "Your inbox is clear. I've flagged 3 emails that need your personal attention.",
    "Meeting brief prepared and sent to all participants 30 minutes ahead.",
    "Done! I've also set a reminder for the follow-up action items.",
  ],
  vera: [
    "Here are 3 caption options for that post. Which tone feels right?",
    "Scheduled across LinkedIn, Twitter, and Instagram for optimal engagement times.",
    "Your post is trending! 847 engagements in the first hour. Want a quick report?",
    "Newsletter draft ready — I've matched your usual voice and included the key points you mentioned.",
  ],
  max: [
    "Research complete! I've compiled a 5-page report with citations. Want the summary?",
    "Found 3 relevant studies that support your hypothesis. Sending them now.",
    "Market analysis ready. Key insight: opportunity window is 6-9 months based on competitor timelines.",
    "Data visualized and added to your Notion dashboard for the team.",
  ],
};
