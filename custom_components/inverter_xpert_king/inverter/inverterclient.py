import socket
import json
from datetime import datetime
from textwrap import wrap
from os import path

from ..inverter.crc16pureclass import *

class InverterClient:
    def __init__(self,  device_ip: str, device_port: int):
        self.device_ip = device_ip
        self.device_port = device_port
        self.connected = False

        self.manufacturer=''
        self.model=''
        self.firmware_version=''

    def connect(self):
        try:
            self.client_socket = socket.socket()
            self.client_socket.settimeout(10)
            self.client_socket.connect((self.device_ip, self.device_port))
            self.connected=True

            self.manufacturer=self.get_model2()
            self.model=self.get_model()
            self.firmware_version=self.get_cpu_firmware_version() + ' / '+ self.get_panel_firmware_version()

            return 1
        except socket.error:
            self.disconnect()
            return 0

    def disconnect(self):
        self.client_socket.close()
        self.connected = False
        return 1
    
    def reconnect(self):
        self.disconnect()
        self.connect()

    def calc_crc(self,command):
        crc_calc = CRC16Calc()
        crc_int=crc_calc.crc16xmodem(command)
        #crc_int=crc16pure.crc16xmodem(command)
        #crc_hex=hex(crc_int)
        crc_bytes=crc_int.to_bytes(2,'big')
        return crc_bytes

    def get_data(self,command,param=''):
        if (self.connected == False):
            self.connect()

        match command:
           case 'QLD':
            if(param==''): # if total energy per day without date set current date
                cur_date=datetime.today().strftime('%Y%m%d')
                param=cur_date            

        message=bytes(command+param,'latin1') + self.calc_crc(command+param) + bytes([13])
        try:
            self.client_socket.send(message)
            data = self.client_socket.recv(1024).decode('latin1')
        except socket.error:
            self.reconnect()
            return []
        data=data.lstrip('(').strip()
        data_size = len(data)
        data = data[:data_size - 2] # remove crc, but maybe will be return later and add crc check to ensure packet data is correct
        #print('Received from server: ' + ' for command ' + command + ' --- '  + data) # for debug
        data_array=data.split(' ')

        match command:
            case "QVFW" | "QVFW3":
              data_array[0] = data_array[0].replace("VERFW:", "")
            case "QVFW2":
              data_array[0] = data_array[0].replace("VERFW2:", "")

        return data_array

    def GetCommandList(self):
        basepath = path.dirname(__file__)
        filepath = basepath + "/inverter_commands.json"
        data_file = open(filepath)
        data = json.load(data_file)
        data_file.close()
        return data
    
    def GetCommandByType(self,command_type):
        command_data=self.GetCommandList()
        command_list={
               'data':['QPIGS','QMOD','QPIWS','QFLAG','QET','QLT','QLD'],
               'conf':['QID','QGMN','QPIRI','QVFW','QVFW3','QVFW2','QMN']
        }

        command_result={}
        for command in command_list[command_type]:
           if command in command_data:
            command_result[command]=command_data[command]

        return command_result

    def GetInverterDataByCommandSync(self,command_type):
        res=[]
        command_list=self.GetCommandByType(command_type)
        
        for command in command_list:
            inverter_data=self.get_data(command)
            received_data=self.process_data(command,inverter_data)
            for received_item in received_data:
                res.append(received_item)
        return res

    async def GetInverterDataByCommand(self,command_type):
        res=[]
        command_list=self.GetCommandByType(command_type)
        
        for command in command_list:
            inverter_data=self.get_data(command)
            received_data=self.process_data(command,inverter_data)
            for received_item in received_data:
                res.append(received_item)
        return res

    def process_data(self,command,inverter_data):
        res=[]
        command_params=self.GetCommandList()[command]

        for index in range(len(inverter_data)):
         try:
          index_str=str(index)

          field_name=command_params[index_str]['name']
          field_type=command_params[index_str]['type']
          field_unit=''
          field_sensor=''
          field_text=''
          if 'unit' in command_params[index_str]:
            field_unit=command_params[index_str]['unit']
          if 'sensor' in command_params[index_str]:
            field_sensor=command_params[index_str]['sensor']
          if 'text' in command_params[index_str]:
            field_text=command_params[index_str]['text']
          else:
            field_text=field_name

          match field_type:
              case "int":
               field_val=int(inverter_data[index])
               if 'multiplier' in command_params[index_str]:
                field_val=field_val*command_params[index_str]['multiplier']

              case "float":
               field_val=float(inverter_data[index])
               if 'multiplier' in command_params[index_str]:
                field_val=field_val*command_params[index_str]['multiplier']

              case "string":
               field_val=inverter_data[index]

              case 'string_binary':
               field_val=inverter_data[index]
               parsed_array_val=self.parse_params_string_binary(field_val, command_params[index_str]['complex_params']['params'])               

              case 'assoc_value':
               field_val=inverter_data[index]
               parsed_array_val=self.parse_params_assoc_value(field_val, command_params[index_str]['complex_params']['params'])               

              case _:
               field_val=inverter_data[index]

          match field_type:
            case 'string_binary' | 'assoc_value':
               for index_parsed in range(len(parsed_array_val)):
                   
                    parsed_item=parsed_array_val[index_parsed]
                    field_name=parsed_item['name']
                    field_sensor=parsed_item['sensor']
                    field_text=''
                    if 'text' in parsed_item:
                        field_text=parsed_item['text']
                    else:
                        field_text=field_name
                    res.append({'param':field_name,'value':parsed_item['value'], 'unit':'','command':command, 'sensor':field_sensor, 'text':field_text})

            case _:
               res.append({'param':field_name,'value':field_val, 'unit':field_unit,'command':command, 'sensor':field_sensor, 'text':field_text})

         except Exception as e:
          error_processing=1

        return res

    def parse_params_string_binary(self,params,param_conf):
        params_array=wrap(params, 1)
        res=[]
        for index in range(len(params_array)):
            value = params_array[index]
            tpl = param_conf[index]
            field_sensor=''
            if 'sensor' in tpl:
                field_sensor=tpl['sensor']
            res.append({
                'name': tpl['name'],
                'text': tpl['text'],
                'value': tpl['value'][value],
                'value_orig': value,
                'sensor': field_sensor
            })
        return res

    def parse_params_assoc_value(self,params,param_conf):
        assoc_value=params
        res=[]
        
        assoc_data=param_conf[0]
        assoc_list=assoc_data['value']
        if assoc_value in assoc_list:
            assoc_result=assoc_list[assoc_value]
        else:
            assoc_result=assoc_value
        
        res.append({
            'name': assoc_data['name'],
            'text': assoc_data['text'],
            'value': assoc_result,
            'value_orig': assoc_value,
            'sensor': assoc_data['sensor']
        })

        return res

    def get_data_single_param(self,command):
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        try:
            return received_data[0]['value']
        except IndexError:
            return ''

    def get_data_single_param_full(self,command):
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        return received_data[0]
    
    def get_serial(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QID')
        else:
            return self.get_data_single_param('QID')

    def get_model2(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QGMN')
        else:
            return self.get_data_single_param('QGMN')

    def get_mode(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QMOD')
        else:
            return self.get_data_single_param('QMOD')    

    def get_warning_status(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QPIWS')
        else:
            return self.get_data_single_param('QPIWS')      

    def get_cpu_firmware_version(self,full=False):
        data=self.get_data_single_param_full('QVFW')
        data['value'] = data['value'].replace("VERFW:", "")
        if(full==True):
           return data
        else:
            return data['value']

    def get_panel_firmware_version(self,full=False):
        data=self.get_data_single_param_full('QVFW3')
        data['value'] = data['value'].replace("VERFW:", "")
        if(full==True):
           return data
        else:
            return data['value']    

    def get_bt_version(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QVFW2')
        else:
            return self.get_data_single_param('QVFW2')   

    def get_flag_status(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QFLAG')
        else:
            return self.get_data_single_param('QFLAG')

    def get_model(self,full=False):
        if(full==True):
           return self.get_data_single_param_full('QMN')
        else:
            return self.get_data_single_param('QMN')

    def get_current_state(self):
        command='QPIGS';
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        return received_data

    def get_current_conf(self):
        command='QPIRI';
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        return received_data

    def get_energy_today(self):
        command='QLD';
        cur_date=datetime.today().strftime('%Y%m%d')
        cur_date_reset_time=datetime.today().strftime('%Y-%m-%d') + ' 00:00:00'
        inverter_data=self.get_data(command,cur_date)
        received_data=self.process_data(command,inverter_data)
        received_data[0]["last_reset"]=cur_date_reset_time
        return received_data[0]

    def get_energy_total(self):
        command='QLT';
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        return received_data[0]

    def set_current_time(self):
        command='DAT'
        cur_date_time=datetime.today().strftime('%y%m%d%H%M%S')
        set_time=self.get_data(command,cur_date_time)
        return (set_time[0]=='AC') # True if inverter respond with ACK
