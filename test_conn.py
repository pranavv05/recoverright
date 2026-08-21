import psycopg

hosts_to_try = [
    "host.docker.internal",
    "127.0.0.1",
    "localhost",
    "172.18.0.1",
    "172.17.0.1",
    "0.0.0.0",
]

for host in hosts_to_try:
    try:
        conn = psycopg.connect(
            host=host,
            port=5432,
            dbname="recoverright",
            user="recoverright",
            password="localpass123",
            connect_timeout=3
        )
        print(f"SUCCESS: {host}")
        conn.close()
        break
    except Exception as e:
        print(f"FAILED {host}: {e}")