admin_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin - HARAMBEE CASH!</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            background: linear-gradient(to right, #43cea2, #185a9d);
            color: white;
        }
        .container {
            width: 90%;
            max-width: 800px;
            padding: 20px;
            background: rgba(0, 0, 0, 0.8);
            border-radius: 15px;
            text-align: center;
            box-sizing: border-box;
        }
        h1 {
            font-size: 2rem;
            margin-bottom: 15px;
            color: #ffcc00;
        }
        h2 {
            font-size: 1.5rem;
            margin-top: 20px;
            color: #ffcc00;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 10px;
            overflow: hidden;
        }
        th, td {
            padding: 10px;
            border: 1px solid #ccc;
            text-align: center;
        }
        th {
            background-color: #4CAF50;
            color: white;
        }
        tr:nth-child(even) {
            background: rgba(255, 255, 255, 0.1);
        }
        form {
            display: flex;
            flex-direction: column;
            gap: 15px;
            text-align: left;
        }
        label {
            font-size: 1rem;
            font-weight: bold;
            color: #ffcccb;
        }
        input, select, button {
            padding: 10px;
            font-size: 1rem;
            border-radius: 5px;
            border: 1px solid #ccc;
            width: 100%;
            box-sizing: border-box;
        }
        input {
            background: rgba(255, 255, 255, 0.1);
            color: white;
        }
        input:focus {
            border-color: #ff9900;
            outline: none;
        }
        select {
            background: rgba(255, 255, 255, 0.1);
            color: white;
        }
        button {
            background-color: #4CAF50;
            color: white;
            cursor: pointer;
            border: none;
            transition: background-color 0.3s ease;
            font-weight: bold;
        }
        button:hover {
            background-color: #45a049;
        }
        .error {
            color: #ffcccb;
            font-weight: bold;
            margin-bottom: 10px;
        }
        a {
            display: inline-block;
            margin-top: 20px;
            color: #ffcc00;
            text-decoration: none;
            font-weight: bold;
            transition: color 0.3s ease;
        }
        a:hover {
            color: #ff9900;
            text-decoration: underline;
        }

        .monitor-btn {
            margin-top: 25px;
            padding: 12px;
            background-color: #ff5722;
            color: white;
            font-weight: bold;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            cursor: pointer;
        }
        .monitor-btn:hover {
            background-color: #e64a19;
        }

        .activity-table th {
            background-color: #3f51b5;
        }
        
        .cashbook-btn {
            margin-top: 25px;
            padding: 12px;
            background-color: #2196F3;
            color: white;
            font-weight: bold;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            cursor: pointer;
        }
        .cashbook-btn:hover {
            background-color: #1976D2;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Admin Dashboard</h1>
        {% if error %} <p class="error">{{ error }}</p> {% endif %}
        {% if message %} <p class="message">{{ message }}</p> {% endif %}

        <h2>All Users</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Username</th>
                    <th>Email</th>
                    <th>Wallet Balance</th>
                </tr>
            </thead>
            <tbody>
                {% for user in users %}
                <tr>
                    <td>{{ user[0] }}</td>
                    <td>{{ user[2] }}</td>
                    <td>{{ user[1] }}</td>
                    <td>Ksh. {{ user[4] | round(2) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Update User Wallet</h2>
        <form method="POST" action="/admin/update_wallet">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label for="user_id">User ID:</label>
            <input type="text" id="user_id" name="user_id" required>
            <label for="amount">Amount:</label>
            <input type="number" id="amount" name="amount" step="0.01" required>
            <label for="action">Action:</label>
            <select id="action" name="action" required>
                <option value="deposit">Deposit</option>
                <option value="withdraw">Withdraw</option>
            </select>
            <button type="submit">Update Wallet</button>
        </form>

        <h2>Add Allowed Username</h2>
        <form method="POST" action="/admin/add_allowed_user">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label for="allowed_username">Username:</label>
            <input type="text" id="allowed_username" name="allowed_username" required>
            <button type="submit">Add Allowed User</button>
        </form>

        <button class="monitor-btn" onclick="window.location.href='/admin/visitor_log'">View Visitor Log</button>
        <button class="cashbook-btn" onclick="window.location.href='/cashbook'">ðŸ’° View Gross Profit Dashboard</button>

        <h2>Recent User Activity (Last 100)</h2>
        <table class="activity-table">
            <thead>
                <tr>
                    <th>Timestamp</th>
                    <th>Username</th>
                    <th>IP</th>
                    <th>Path</th>
                    <th>Method</th>
                    <th>User Agent</th>
                    <th>Referrer</th>
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                <tr>
                    <td>{{ log[1] }}</td>
                    <td>{{ log[2] or 'Guest' }}</td>
                    <td>{{ log[3] }}</td>
                    <td>{{ log[4] }}</td>
                    <td>{{ log[5] }}</td>
                    <td>{{ log[6][:60] }}{% if log[6]|length > 60 %}...{% endif %}</td>
                    <td>{{ log[7] or '-' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        
        <button class="cashbook-btn" onclick="window.location.href='/admin/withdrawals'" 
                style="background-color: #9C27B0; margin-top: 15px;">
            ðŸ’³ Manage Withdrawals
        </button>
        
        <a href="/admin/logout">Logout</a>
    </div>
    <script>
    // Auto-refresh admin data every 1 hour
    function refreshAdminData() {
        location.reload();
    }

    // 1 hour = 60 minutes * 60 seconds * 1000 milliseconds
    setTimeout(refreshAdminData, 3600000);

    // Auto-clear admin messages after 1 hour
    setTimeout(() => {
        const errorElements = document.querySelectorAll('.error');
        const messageElements = document.    querySelectorAll('.message');
    
        errorElements.forEach(el => el.style.display = 'none');
        messageElements.forEach(el => el.style.display = 'none');
    }, 3600000);
    </script>
</body>
</html>
"""
