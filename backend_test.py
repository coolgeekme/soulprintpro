#!/usr/bin/env python3
"""
JWT Authentication Backend Testing for Emergent Wingman Platform
Tests all authentication endpoints with proper cookie handling
"""

import requests
import json
import sys
from datetime import datetime

# Backend URL from environment
BACKEND_URL = "https://emergent-wing.preview.emergentagent.com/api"

# Test credentials
ADMIN_EMAIL = "admin@wingman.ai"
ADMIN_PASSWORD = "admin123"

# Test user for registration
TEST_USER = {
    "name": "Test User",
    "email": "test@example.com", 
    "password": "test123"
}

class AuthTester:
    def __init__(self):
        self.session = requests.Session()
        self.results = []
        
    def log_result(self, test_name, success, details):
        """Log test result"""
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if details:
            print(f"   Details: {details}")
        self.results.append({
            "test": test_name,
            "success": success,
            "details": details,
            "timestamp": datetime.now().isoformat()
        })
        
    def test_login_valid_credentials(self):
        """Test 1: POST /api/auth/login with valid credentials"""
        try:
            response = self.session.post(
                f"{BACKEND_URL}/auth/login",
                json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                # Check response structure
                if all(key in data for key in ["id", "name", "email"]):
                    # Check cookies
                    cookies = response.cookies
                    if "access_token" in cookies and "refresh_token" in cookies:
                        # Verify httpOnly cookies
                        access_cookie = cookies.get("access_token")
                        refresh_cookie = cookies.get("refresh_token")
                        self.log_result(
                            "Login with valid credentials",
                            True,
                            f"User: {data['email']}, Cookies: access_token={bool(access_cookie)}, refresh_token={bool(refresh_cookie)}"
                        )
                        return True
                    else:
                        self.log_result("Login with valid credentials", False, "Missing JWT cookies")
                else:
                    self.log_result("Login with valid credentials", False, f"Invalid response structure: {data}")
            else:
                self.log_result("Login with valid credentials", False, f"Status {response.status_code}: {response.text}")
                
        except Exception as e:
            self.log_result("Login with valid credentials", False, f"Exception: {str(e)}")
        return False
        
    def test_login_invalid_credentials(self):
        """Test 2: POST /api/auth/login with wrong password"""
        try:
            response = self.session.post(
                f"{BACKEND_URL}/auth/login",
                json={"email": ADMIN_EMAIL, "password": "wrongpassword"},
                timeout=10
            )
            
            if response.status_code == 401:
                data = response.json()
                if "Invalid email or password" in data.get("detail", ""):
                    self.log_result("Login with invalid credentials", True, "Correctly rejected with 401")
                    return True
                else:
                    self.log_result("Login with invalid credentials", False, f"Wrong error message: {data}")
            else:
                self.log_result("Login with invalid credentials", False, f"Expected 401, got {response.status_code}")
                
        except Exception as e:
            self.log_result("Login with invalid credentials", False, f"Exception: {str(e)}")
        return False
        
    def test_me_with_cookie(self):
        """Test 3: GET /api/auth/me WITH cookie from login"""
        try:
            response = self.session.get(f"{BACKEND_URL}/auth/me", timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if all(key in data for key in ["id", "name", "email"]):
                    self.log_result("Get current user with cookie", True, f"User: {data['email']}")
                    return True
                else:
                    self.log_result("Get current user with cookie", False, f"Invalid user data: {data}")
            else:
                self.log_result("Get current user with cookie", False, f"Status {response.status_code}: {response.text}")
                
        except Exception as e:
            self.log_result("Get current user with cookie", False, f"Exception: {str(e)}")
        return False
        
    def test_me_without_cookie(self):
        """Test 4: GET /api/auth/me WITHOUT cookie"""
        try:
            # Create new session without cookies
            fresh_session = requests.Session()
            response = fresh_session.get(f"{BACKEND_URL}/auth/me", timeout=10)
            
            if response.status_code == 401:
                data = response.json()
                if "Not authenticated" in data.get("detail", ""):
                    self.log_result("Get current user without cookie", True, "Correctly rejected with 401")
                    return True
                else:
                    self.log_result("Get current user without cookie", False, f"Wrong error message: {data}")
            else:
                self.log_result("Get current user without cookie", False, f"Expected 401, got {response.status_code}")
                
        except Exception as e:
            self.log_result("Get current user without cookie", False, f"Exception: {str(e)}")
        return False
        
    def test_register_new_user(self):
        """Test 5: POST /api/auth/register with new user"""
        try:
            # Use a unique email to avoid conflicts
            unique_email = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}@example.com"
            test_data = {
                "name": TEST_USER["name"],
                "email": unique_email,
                "password": TEST_USER["password"]
            }
            
            response = self.session.post(
                f"{BACKEND_URL}/auth/register",
                json=test_data,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if all(key in data for key in ["id", "name", "email"]):
                    # Check cookies
                    cookies = response.cookies
                    if "access_token" in cookies and "refresh_token" in cookies:
                        self.log_result("Register new user", True, f"User created: {data['email']}")
                        return True
                    else:
                        self.log_result("Register new user", False, "Missing JWT cookies after registration")
                else:
                    self.log_result("Register new user", False, f"Invalid response structure: {data}")
            else:
                self.log_result("Register new user", False, f"Status {response.status_code}: {response.text}")
                
        except Exception as e:
            self.log_result("Register new user", False, f"Exception: {str(e)}")
        return False
        
    def test_register_duplicate_email(self):
        """Test 6: POST /api/auth/register with existing email"""
        try:
            response = self.session.post(
                f"{BACKEND_URL}/auth/register",
                json={"name": "Duplicate", "email": ADMIN_EMAIL, "password": "test123"},
                timeout=10
            )
            
            if response.status_code == 400:
                data = response.json()
                if "Email already registered" in data.get("detail", ""):
                    self.log_result("Register duplicate email", True, "Correctly rejected with 400")
                    return True
                else:
                    self.log_result("Register duplicate email", False, f"Wrong error message: {data}")
            else:
                self.log_result("Register duplicate email", False, f"Expected 400, got {response.status_code}")
                
        except Exception as e:
            self.log_result("Register duplicate email", False, f"Exception: {str(e)}")
        return False
        
    def test_logout(self):
        """Test 7: POST /api/auth/logout"""
        try:
            response = self.session.post(f"{BACKEND_URL}/auth/logout", timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if "Logged out" in data.get("message", ""):
                    # Check if cookies are cleared (should be empty or expired)
                    self.log_result("Logout", True, "Successfully logged out")
                    return True
                else:
                    self.log_result("Logout", False, f"Wrong response message: {data}")
            else:
                self.log_result("Logout", False, f"Status {response.status_code}: {response.text}")
                
        except Exception as e:
            self.log_result("Logout", False, f"Exception: {str(e)}")
        return False
        
    def test_me_after_logout(self):
        """Test 8: GET /api/auth/me after logout"""
        try:
            response = self.session.get(f"{BACKEND_URL}/auth/me", timeout=10)
            
            if response.status_code == 401:
                data = response.json()
                if "Not authenticated" in data.get("detail", ""):
                    self.log_result("Get current user after logout", True, "Correctly rejected with 401")
                    return True
                else:
                    self.log_result("Get current user after logout", False, f"Wrong error message: {data}")
            else:
                self.log_result("Get current user after logout", False, f"Expected 401, got {response.status_code}")
                
        except Exception as e:
            self.log_result("Get current user after logout", False, f"Exception: {str(e)}")
        return False
        
    def run_all_tests(self):
        """Run all authentication tests in sequence"""
        print(f"🚀 Starting JWT Authentication Tests")
        print(f"Backend URL: {BACKEND_URL}")
        print(f"Test Credentials: {ADMIN_EMAIL}")
        print("=" * 60)
        
        # Test sequence - order matters for cookie handling
        tests = [
            self.test_login_valid_credentials,
            self.test_login_invalid_credentials, 
            self.test_me_with_cookie,
            self.test_me_without_cookie,
            self.test_register_new_user,
            self.test_register_duplicate_email,
            self.test_logout,
            self.test_me_after_logout
        ]
        
        passed = 0
        total = len(tests)
        
        for test in tests:
            if test():
                passed += 1
            print()  # Add spacing between tests
            
        print("=" * 60)
        print(f"📊 Test Results: {passed}/{total} tests passed")
        
        if passed == total:
            print("🎉 All authentication tests PASSED!")
            return True
        else:
            print(f"⚠️  {total - passed} test(s) FAILED")
            return False

def main():
    """Main test runner"""
    tester = AuthTester()
    success = tester.run_all_tests()
    
    # Save detailed results
    with open("/app/auth_test_results.json", "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "backend_url": BACKEND_URL,
            "total_tests": len(tester.results),
            "passed_tests": sum(1 for r in tester.results if r["success"]),
            "results": tester.results
        }, f, indent=2)
    
    print(f"\n📄 Detailed results saved to: /app/auth_test_results.json")
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()