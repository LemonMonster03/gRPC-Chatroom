import grpc
import chat_pb2
import chat_pb2_grpc
import threading
import time
import queue


class ChatClient:
    def __init__(self):
        self.channel = grpc.insecure_channel('localhost:50051')
        self.stub = chat_pb2_grpc.ChatServiceStub(self.channel)
        # 本地信息
        self.userName = None
        self.send_queue = queue.Queue()
        self.chat_stream = None
        self.online_users = []
        self.stop_flag = False

    def gen_msg(self):
        """生成器：从队列中获取消息并发送"""
        while not self.stop_flag:
            try:
                # 1s内无消息抛出异常
                msg = self.send_queue.get(timeout=1)
                # 空消息，说明上一次发的是停止信号
                if msg is None:
                    break
                # 读取到消息，返回消息
                yield msg
            except queue.Empty:
                # 检测stop_flag
                continue

    def register(self):
        """注册用户"""
        self.userName = input("请输入用户名: ").strip()
        if not self.userName:
            print("用户名不能为空")
            return False

        try:
            response = self.stub.Register(chat_pb2.RegisterMsg(userName=self.userName))

            if not response.status:
                print(f"【错误】{response.reason}")
                return False

            self.online_users = list(response.OnlineList)
            print(f"\n欢迎 {self.userName} 加入服务器！")
            print(f"当前在线用户: {', '.join(self.online_users)}\n")
            return True
        except grpc.RpcError as e:
            print(f"【错误】注册失败: {e}")
            return False

    def recv_msg(self):
        """接收消息线程"""
        try:
            for msg in self.chat_stream:
                if msg.send_name == "Server":
                    # 服务器消息
                    if msg.content.startswith("【在线用户】"):
                        # 更新在线列表
                        online_str = msg.content.replace("【在线用户】", "")
                        if online_str != "暂无用户":
                            self.online_users = [u.strip() for u in online_str.split(',')]
                        else:
                            self.online_users = []
                        print(f"\n{msg.content}")
                    elif msg.content.startswith("【系统通知】"):
                        # 系统通知
                        print(f"\n{msg.content}")
                    else:
                        print(f"\n【{msg.send_name}】{msg.content}")
                else:
                    # 来自其他用户的消息
                    print(f"\n【{msg.send_name}】悄悄对您说：{msg.content}")
                # 重新显示提示符
                if not self.stop_flag:
                    print(">>> ", end='', flush=True)

        except Exception as e:
            if not self.stop_flag:
                print(f"\n【错误】接收消息异常: {e}")

    def send_first_msg(self):
        """发送第一条消息,初始化流"""
        init_msg = chat_pb2.ChatMsg(send_name=self.userName, recv_name="", content="")
        self.send_queue.put(init_msg)

    def show_help(self):
        """显示帮助信息"""
        print("\n" + "=" * 50)
        print("命令列表:")
        print("  /chat <username>  - 与指定用户私聊")
        print("  /all              - 进入广播模式（下一条消息广播给所有人）")
        print("  /list             - 显示当前在线用户")
        print("  /exit             - 退出连接")
        print("  /help             - 显示此帮助信息")
        print("=" * 50 + "\n")

    def run(self):
        """主程序"""
        if not self.register():
            return

        try:
            # 初始化聊天流
            # 双向流式连接
            self.chat_stream = self.stub.Chat(self.gen_msg())

            # 启动接收消息线程
            recv_thread = threading.Thread(target=self.recv_msg, daemon=True)
            recv_thread.start()

            # 发送初始化消息
            self.send_first_msg()

            # 等待流初始化
            time.sleep(0.5)
            self.show_help()

            # 主循环：发送消息
            cur_target = None   # 消息对象
            while True:
                try:
                    if cur_target is None:
                        prompt = ">>> "
                    else:
                        prompt = f"[发往 {cur_target}] >>> "

                    user_input = input(prompt).strip()

                    if not user_input:
                        continue

                    # 处理命令
                    if user_input.lower() == "/exit":
                        self.stop_flag = True
                        msg = chat_pb2.ChatMsg(send_name=self.userName, recv_name="/exit", content="")
                        self.send_queue.put(msg)
                        self.send_queue.put(None)  # 信号停止
                        time.sleep(0.5)
                        print("已断开连接")
                        break
                    elif user_input.lower() == "/help":
                        self.show_help()
                    elif user_input.lower() == "/list":
                        print(f"当前在线用户: {', '.join(self.online_users)}")
                    elif user_input.lower().startswith("/chat "):
                        parts = user_input.split(" ", 1)
                        if len(parts) < 2:
                            print("【错误】请指定用户名: /chat <username>")
                            continue
                        target = parts[1].strip()
                        if target not in self.online_users:
                            print(f"【错误】{target} 不在线，当前在线用户: {', '.join(self.online_users)}")
                        else:
                            cur_target = target
                            print(f"已切换聊天对象为: {cur_target}")
                    elif user_input.lower() == "/all":
                        content = input("请输入广播消息: ").strip()
                        if content:
                            msg = chat_pb2.ChatMsg(send_name=self.userName, recv_name="/all", content=content)
                            self.send_queue.put(msg)
                        # 可选：清空当前聊天对象
                        cur_target = None
                    else:
                        # 普通消息
                        if cur_target is None:
                            print("【错误】请先用 /chat <username> 选择聊天对象")
                        else:
                            # 检查目标用户是否还在线
                            if cur_target not in self.online_users:
                                print(f"【错误】{cur_target} 已离线")
                                # 可选：清空当前聊天对象
                                cur_target = None
                            else:
                                msg = chat_pb2.ChatMsg(send_name=self.userName, recv_name=cur_target, content=user_input)
                                self.send_queue.put(msg)
                except KeyboardInterrupt:
                    print("\n已断开连接")
                    self.stop_flag = True
                    break
                except EOFError:
                    print("\n已断开连接")
                    self.stop_flag = True
                    break
                except Exception as e:
                    print(f"【错误】{e}")

        except Exception as e:
            print(f"【错误】连接失败: {e}")
        finally:
            self.stop_flag = True
            self.channel.close()


if __name__ == '__main__':
    client = ChatClient()
    client.run()
