import grpc
from concurrent import futures
import time
import threading
import chat_pb2
import chat_pb2_grpc


class ChatService(chat_pb2_grpc.ChatServiceServicer):
    def __init__(self):
        # 在线用户列表
        self.online_user = []
        # 各用户消息队列
        self.users = {}
        self.lock = threading.Lock()

    def logout(self, username):
        """登出处理"""
        with (self.lock):
            if username in self.online_user:
                self.online_user.remove(username)
                # 广播用户离线消息
                if self.online_user:
                    user_list = ', '.join(self.online_user)
                else:
                    user_list = "暂无用户"
                leave_msg = f"【系统通知】{username} 已离线"

                # 通知所有在线用户
                for user, queue in self.users.items():
                    if queue is not None and user != username:
                        queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=user, content=leave_msg))
                        queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=user, content=f"【在线用户】{user_list}"))

                # 删除该用户的队列
                self.users.pop(username, None)
                print(f"[Log]{username} 登出")

    def broadcast_online_list(self):
        """广播在线用户列表"""
        if self.online_user:
            user_list = ', '.join(self.online_user)
        else:
            user_list = "暂无用户"

        with self.lock:
            # 通知所有用户
            for user, queue in self.users.items():
                if queue is not None:
                    queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=user, content=f"【在线用户】{user_list}"))

    def broadcast_user_join(self, new_user):
        """广播新用户加入"""
        join_msg = f"【系统通知】{new_user} 已上线"
        user_list = ', '.join(self.online_user) if self.online_user else "暂无用户"

        with self.lock:
            # 通知所有用户
            for user, queue in self.users.items():
                if queue is not None and user != new_user:
                    queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=user, content=join_msg))
                    queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=user, content=f"【在线用户】{user_list}"))

    def Register(self, request, context):
        """用户名注册"""
        name = request.userName.strip()
        with self.lock:
            if name in self.online_user:
                return chat_pb2.RegisterResponse(status=False, reason="用户已存在", OnlineList=self.online_user)
            self.online_user.append(name)
            self.users[name] = None
            print(f"[Log]{name} 注册")

        return chat_pb2.RegisterResponse(status=True, OnlineList=self.online_user)

    def Chat(self, request_iterator, context):
        """核心部分"""
        userName = None
        msg_queue = None
        # 断联信号量
        stop_event = threading.Event()

        def receive_messages():
            """接受消息"""
            try:
                # 阻塞接收
                for message in request_iterator:
                    """命令检查"""
                    # 退出
                    if message.recv_name == "/exit":
                        print(f"{userName} 退出")
                        # 通知主循环停止
                        stop_event.set()
                        return
                    # 广播
                    if message.recv_name == "/all":
                        # 广播给所有用户
                        with self.lock:
                            for user, queue in self.users.items():
                                if queue is not None and user != userName:
                                    queue.append(chat_pb2.ChatMsg(send_name=userName, recv_name=user, content=f"【广播】{message.content}"))
                        # 给发送者反馈
                        msg_queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=userName, content=f"消息已广播给所有在线用户"))
                        continue
                    # 私聊消息
                    with self.lock:
                        if message.recv_name in self.online_user:
                            recv_queue = self.users.get(message.recv_name)
                            if recv_queue is not None:
                                recv_queue.append(chat_pb2.ChatMsg(send_name=userName, recv_name=message.recv_name, content=message.content))
                                print(f"[Log] {userName} 向 {message.recv_name} 发送消息 : {message.content}")
                            else:
                                # 用户队列无效（可能正在断开）
                                msg_queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=userName, content=f"【错误】{message.recv_name} 当前不在线！"))
                        else:
                            msg_queue.append(chat_pb2.ChatMsg(send_name="Server", recv_name=userName, content=f"【错误】{message.recv_name} 当前不在线！"))
            except grpc.RpcError as e:
                print(f"[RPC ERROR] {userName}: {e}")
                stop_event.set()
            except Exception as e:
                print(f"[ERROR] {userName}: {e}")
                stop_event.set()

        try:
            # 初始化用户的消息队列
            first_msg = next(request_iterator)
            userName = first_msg.send_name
            msg_queue = []
            with self.lock:
                self.users[userName] = msg_queue

            print(f"[Log]{userName} 连接")

            # 发送欢迎消息
            yield chat_pb2.ChatMsg(send_name="Server", recv_name=userName, content=f"Hi, {userName}! 欢迎加入")

            # 发送当前在线列表
            if self.online_user:
                user_list = ', '.join(self.online_user)
            else:
                user_list = "暂无用户"
            yield chat_pb2.ChatMsg(send_name="Server", recv_name=userName, content=f"【在线用户】{user_list}")

            # 广播新用户加入
            self.broadcast_user_join(userName)

            # 启动接收线程
            # 将消息放入消息队列
            recv_thread = threading.Thread(target=receive_messages, daemon=True)
            recv_thread.start()

            # 主线程，轮询发送消息
            # 信号量通知 or 连接中断 才退出循环
            while not stop_event.is_set() and context.is_active():
                if msg_queue:
                    # 发送消息
                    while len(msg_queue) > 0:
                        msg = msg_queue.pop(0)
                        yield msg
                # 避免CPU空转
                time.sleep(0.1)

        except grpc.RpcError as e:
            print(f"[RPC ERROR] {userName}: {e}")
        except StopIteration:
            print(f"{userName} connection ended (StopIteration)")
        except Exception as e:
            print(f"[ERROR] {userName}: {e}")
        finally:
            # 注销用户
            if userName:
                print(f"[Log] 注销用户 {userName}")
                self.logout(userName)
            stop_event.set()


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    chat_pb2_grpc.add_ChatServiceServicer_to_server(ChatService(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    print("Server started on port 50051")

    try:
        while True:
            time.sleep(600)
    except KeyboardInterrupt:
        server.stop(0)


if __name__ == '__main__':
    serve()
