$PemKey = "D:\Study\Projects\Raikou\RAIKOU.pem"
$Ec2User = "ubuntu"
$Ec2Host = "13.203.213.241"
$LocalBackend = "D:\Study\Projects\Raikou\backend"
$RemoteBackend = "/home/ubuntu/backend"

Write-Host "Deploying backend to EC2..."
scp -i $PemKey -o StrictHostKeyChecking=no -r $LocalBackend\app $Ec2User@${Ec2Host}:$RemoteBackend
scp -i $PemKey -o StrictHostKeyChecking=no -r $LocalBackend\scripts $Ec2User@${Ec2Host}:$RemoteBackend
scp -i $PemKey -o StrictHostKeyChecking=no $LocalBackend\requirements.txt $Ec2User@${Ec2Host}:$RemoteBackend
scp -i $PemKey -o StrictHostKeyChecking=no $LocalBackend\main.py $Ec2User@${Ec2Host}:$RemoteBackend

Write-Host "Deployment complete."
