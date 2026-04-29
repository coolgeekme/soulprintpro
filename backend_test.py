#!/usr/bin/env python3
"""
Backend API Testing Script for Emergent Wingman Platform
Tests AI chat endpoints with persistent MongoDB history
"""

import requests
import json
import time
from datetime import datetime

# Configuration
BACKEND_URL = "https://emergent-wing.preview.emergentagent.com/api"
ADMIN_EMAIL = "admin@wingman.ai"
ADMIN_PASSWORD = "admin123"

class WingmanAPITester:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'WingmanTester/1.0'
        })
        self.auth_cookies = None
        
    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {level}: {message}")
        
    def test_login(self):
        """Test 1: Login to get auth cookie"""
        self.log("Testing login authentication...")
        
        login_data = {
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        }
        
        try:
            response = self.session.post(f"{BACKEND_URL}/auth/login", json=login_data)
            
            if response.status_code == 200:
                data = response.json()
                self.log(f"✅ Login successful: {data}")
                self.log(f"Cookies received: {list(response.cookies.keys())}")
                return True
            else:
                self.log(f"❌ Login failed: {response.status_code} - {response.text}", "ERROR")
                return False
                
        except Exception as e:
            self.log(f"❌ Login error: {str(e)}", "ERROR")
            return False
    
    def test_get_messages_empty(self, agent_id="jarvis"):
        """Test 2: GET /api/chat/{agent_id} - should return [] or existing messages"""
        self.log(f"Testing GET /api/chat/{agent_id} (load message history)...")
        
        try:
            response = self.session.get(f"{BACKEND_URL}/chat/{agent_id}")
            
            if response.status_code == 200:
                messages = response.json()
                self.log(f"✅ Messages loaded: {len(messages)} messages found")
                if messages:
                    self.log(f"Sample message: {messages[0]}")
                return True, messages
            else:
                self.log(f"❌ Get messages failed: {response.status_code} - {response.text}", "ERROR")
                return False, []
                
        except Exception as e:
            self.log(f"❌ Get messages error: {str(e)}", "ERROR")
            return False, []
    
    def test_send_message(self, agent_id="jarvis", message_text="What Python framework would you recommend for a REST API?"):
        """Test 3: POST /api/chat/{agent_id} - send message and get AI response"""
        self.log(f"Testing POST /api/chat/{agent_id} (send message + AI response)...")
        
        message_data = {"text": message_text}
        
        try:
            response = self.session.post(f"{BACKEND_URL}/chat/{agent_id}", json=message_data)
            
            if response.status_code == 200:
                data = response.json()
                user_msg = data.get("user_message", {})
                agent_msg = data.get("agent_message", {})
                
                self.log(f"✅ Message sent successfully")
                self.log(f"User message: {user_msg.get('text', '')[:100]}...")
                self.log(f"Agent response: {agent_msg.get('text', '')[:100]}...")
                
                # Check if response is real AI (not hardcoded)
                agent_text = agent_msg.get('text', '')
                if len(agent_text) > 50 and not agent_text.startswith("I'm having a bit of trouble"):
                    self.log("✅ Real AI response detected (not hardcoded)")
                else:
                    self.log("⚠️ Response might be fallback/error message")
                
                return True, data
            else:
                self.log(f"❌ Send message failed: {response.status_code} - {response.text}", "ERROR")
                return False, None
                
        except Exception as e:
            self.log(f"❌ Send message error: {str(e)}", "ERROR")
            return False, None
    
    def test_message_persistence(self, agent_id="jarvis"):
        """Test 4: GET /api/chat/{agent_id} again - verify messages persist"""
        self.log(f"Testing message persistence for {agent_id}...")
        
        success, messages = self.test_get_messages_empty(agent_id)
        if success and len(messages) >= 2:
            self.log(f"✅ Messages persisted: {len(messages)} messages in history")
            return True
        else:
            self.log(f"❌ Message persistence failed: only {len(messages) if success else 0} messages found", "ERROR")
            return False
    
    def test_different_agents(self):
        """Test 5-6: Test different agent personalities"""
        agents_to_test = [
            ("judy", "How do I write a cold email to a B2B prospect?"),
            ("alfred", "Draft a meeting cancellation email for me")
        ]
        
        results = []
        for agent_id, message in agents_to_test:
            self.log(f"Testing {agent_id} personality...")
            success, response = self.test_send_message(agent_id, message)
            results.append((agent_id, success, response))
            time.sleep(1)  # Brief pause between requests
        
        return results
    
    def test_invalid_agent(self):
        """Test 7: POST /api/chat/invalid_agent - should return 404"""
        self.log("Testing invalid agent endpoint...")
        
        message_data = {"text": "test"}
        
        try:
            response = self.session.post(f"{BACKEND_URL}/chat/invalid_agent", json=message_data)
            
            if response.status_code == 404:
                self.log("✅ Invalid agent correctly returns 404")
                return True
            else:
                self.log(f"❌ Invalid agent test failed: expected 404, got {response.status_code}", "ERROR")
                return False
                
        except Exception as e:
            self.log(f"❌ Invalid agent test error: {str(e)}", "ERROR")
            return False
    
    def test_clear_conversation(self, agent_id="jarvis"):
        """Test 9: DELETE /api/chat/{agent_id} - clear conversation"""
        self.log(f"Testing DELETE /api/chat/{agent_id} (clear conversation)...")
        
        try:
            response = self.session.delete(f"{BACKEND_URL}/chat/{agent_id}")
            
            if response.status_code == 200:
                data = response.json()
                self.log(f"✅ Conversation cleared: {data}")
                return True
            else:
                self.log(f"❌ Clear conversation failed: {response.status_code} - {response.text}", "ERROR")
                return False
                
        except Exception as e:
            self.log(f"❌ Clear conversation error: {str(e)}", "ERROR")
            return False
    
    def test_cleared_messages(self, agent_id="jarvis"):
        """Test 10: GET /api/chat/{agent_id} - should return [] after clear"""
        self.log(f"Testing messages after clear for {agent_id}...")
        
        success, messages = self.test_get_messages_empty(agent_id)
        if success and len(messages) == 0:
            self.log("✅ Messages successfully cleared")
            return True
        else:
            self.log(f"❌ Clear verification failed: {len(messages) if success else 'error'} messages still found", "ERROR")
            return False
    
    def run_all_tests(self):
        """Run the complete test suite"""
        self.log("=" * 60)
        self.log("STARTING EMERGENT WINGMAN BACKEND API TESTS")
        self.log("=" * 60)
        
        test_results = {}
        
        # Test 1: Login
        test_results['login'] = self.test_login()
        if not test_results['login']:
            self.log("❌ Cannot proceed without authentication", "ERROR")
            return test_results
        
        # Test 2: Get initial messages
        test_results['get_messages_initial'] = self.test_get_messages_empty("jarvis")[0]
        
        # Test 3: Send message to Jarvis
        test_results['send_jarvis'] = self.test_send_message("jarvis")[0]
        
        # Test 4: Verify persistence
        test_results['persistence'] = self.test_message_persistence("jarvis")
        
        # Test 5-6: Different agents
        agent_results = self.test_different_agents()
        test_results['judy'] = agent_results[0][1] if len(agent_results) > 0 else False
        test_results['alfred'] = agent_results[1][1] if len(agent_results) > 1 else False
        
        # Test 7: Invalid agent
        test_results['invalid_agent'] = self.test_invalid_agent()
        
        # Test 8: Check Jarvis messages still persist
        test_results['jarvis_persistence_check'] = self.test_message_persistence("jarvis")
        
        # Test 9: Clear conversation
        test_results['clear_conversation'] = self.test_clear_conversation("jarvis")
        
        # Test 10: Verify cleared
        test_results['verify_cleared'] = self.test_cleared_messages("jarvis")
        
        # Summary
        self.log("=" * 60)
        self.log("TEST RESULTS SUMMARY")
        self.log("=" * 60)
        
        passed = sum(1 for result in test_results.values() if result)
        total = len(test_results)
        
        for test_name, result in test_results.items():
            status = "✅ PASS" if result else "❌ FAIL"
            self.log(f"{test_name}: {status}")
        
        self.log(f"\nOVERALL: {passed}/{total} tests passed")
        
        if passed == total:
            self.log("🎉 ALL TESTS PASSED! Backend API is working correctly.")
        else:
            self.log(f"⚠️ {total - passed} tests failed. Check logs above for details.")
        
        return test_results

if __name__ == "__main__":
    tester = WingmanAPITester()
    results = tester.run_all_tests()