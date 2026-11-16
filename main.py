#_______________________________________________________________________________________
#               LIBRARIES
#_______________________________________________________________________________________

from colorama import init, Fore as COLOR #CHANGES COLORS OF PRINTED TEXT
import pymupdf
import os
import shutil
import img_to_midi as ImgToMidiAI       #img_to_midi  was completly written by AI models
import music_player as MusicPlayerAI    #music_player was completly written by AI models



#_______________________________________________________________________________________
#               Function definitions
#_______________________________________________________________________________________

def pdf_to_jpeg(filename):
    # Converts a file from pdf to images and returns the number of pages
    with pymupdf.open(filename) as pdf_file:
        for page in range(pdf_file.page_count):
            cur_page = pdf_file.load_page(page)
            cur_image = cur_page.get_pixmap(dpi=300)
            cur_image.save(f"{DES_path}image_{page}.jpeg") # saves the image to Program folder
            imgList.append(f"{DES_path}image_{page}.jpeg")
            print(COLOR.YELLOW + f"\tPage number {page+1} saved successfuly") 
        return pdf_file.page_count

    
#_______________________________________________________________________________________
#               INITIALIZATION
init(autoreset=True) # resets the color of printed text after each print

SRC_path = "MusicSheets/"       #Source path 
DES_path = "ProgramRun/"        #Destination - Directory
#TAR_path                       #Target -       Specific file
AUDIVERIS_path = "Dependencies/Audiveris/Audiveris.exe"
MidiPath = f"{DES_path}music.midi"

os.makedirs(DES_path, exist_ok=True) # Creates a folder named ProgramRun and if the folder exists pass

filesInFolder = os.listdir(DES_path)
for file in filesInFolder:
    os.remove(DES_path+file)

class Music:
    def __init__(self, pitch, duration, relative_start_time):
        self.pitch = pitch
        self.dur = duration
        self.stime = relative_start_time



#_______________________________________________________________________________________
#               FILE EXTRACTION AND RECOGNITION

# 1) Obtain the midi file from supported file formats


FileWorksWell = False
while FileWorksWell == False: \

    imgList = []

    filename = input("Please enter file name ").strip()
    extention = ((filename.split("."))[-1]).lower()
    

    try:
        TAR_path = SRC_path+filename

        #check if file opens  
        with open(TAR_path, "rb"):
            pass

        # if its a pdf convert to image
        if extention == "pdf":

            #FileWorksWell = True
            print(COLOR.GREEN + "PDF file found\n")
            print(COLOR.YELLOW + "Converting PDF file to image/s")
            page_count = pdf_to_jpeg(TAR_path)

            if page_count == 1: # only one page was found
                print(COLOR.YELLOW + f"{page_count} page was converted successfuly\n")
            elif page_count > 1: # multiple pages were found
                print(COLOR.YELLOW + f"{page_count} pages were converted successfuly\n")
            else:
                print(COLOR.RED + "An error ocurred when trying to convert pdf")


        # if its an image convert to midi
        if extention in ("png", "jpg", "jpeg") or len(imgList) > 0:
            # convert to midi
            FileWorksWell = True

            if len(imgList) == 0:
                print(COLOR.GREEN + "Image file found")
                shutil.copy2(TAR_path, DES_path+filename)
                imgList.append(DES_path+filename)

            print(COLOR.YELLOW + "Converting image/s to a midi file")

            ImgToMidiAI.image_to_midi(   imgList,                            #Src:  list of images
                                MidiPath,                           #dest: name of file created
                                audiveris_path=AUDIVERIS_path,)     # Path to Audiveris.exe

            print(COLOR.YELLOW + "Image/s converted to a midi file successfuly\n")
            extention = "midi"


        # if its midi then run the program
        if (extention == "midi") or (extention == "mid") or (len(imgList)>0):
            if len(imgList) == 0: #originally a midi file
                FileWorksWell = True
                print(COLOR.GREEN + "MIDI file found")
                shutil.copy2(TAR_path, MidiPath)
            # run the actual program
            break #We have a midi file no need to continue looping
        else: 
            print(COLOR.RED + "Incorrect file name or unsupported file format please run the program again and ensure file is in a supported format")
            print(COLOR.RED + "supported formats include: PNG, JPEG, JPG, MIDI, MID, PDF",)

    except FileNotFoundError:
        print(COLOR.RED + "Error: File not found")
        print(COLOR.RED + "Please try again\n\n")
    except OSError:
        print(COLOR.RED + "Error: Could not open file")
        print(COLOR.RED + "Please try again\n\n")



#_______________________________________________________________________________________
#               Finalizing

while True:
    selected_mode = input("Mode [0 = sheet music, 1 = MIDI playback, 2 = optimized lists] (default 1): ").strip() or "1"
    if selected_mode in ("0", "1", "2"):
        break
    print("Enter 0, 1, or 2.")

mode = int(selected_mode)
score_path = MusicPlayerAI.play(MidiPath, mode=mode, instrument="piano")
if mode == 0:
    print(f"Sheet music saved to {score_path}")
